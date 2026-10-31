"""Install a bound ARM64 entry trampoline in verified unused Mach-O padding.

No runtime executable-memory writes. The original game entry remains intact.
This is intentionally limited to the audited classic dyld-info Mach-O layout.
"""
import struct
from macho import MachO

def uleb(value):
    out = bytearray()
    while value >= 128:
        out.append((value & 127) | 128)
        value >>= 7
    out.append(value)
    return out

def bind_end(stream):
    p = 0
    while p < len(stream):
        at = p
        op = stream[p] & 0xf0
        p += 1
        if op == 0:
            return at
        if op == 0x40:
            p = stream.index(0, p)+1
        elif op in (0x20,0x60,0x70,0x80,0xa0,0xc0):
            for _ in range(2 if op == 0xc0 else 1):
                while stream[p] & 128:
                    p += 1
                p += 1
        elif op not in (0x10,0x30,0x50,0x90,0xb0):
            raise ValueError(f'Unsupported bind opcode {op:x}')
    raise ValueError('No BIND_DONE')

def inject_entry(path, sdl_path=None):
    m = MachO(path)
    data = m.data
    assert all(c != 0x1d for c,p,n in m.commands), 'Inject before codesign'
    segments = [(p, bytes(data[p+8:p+24]).rstrip(b'\0')) for c,p,n in m.commands if c==0x19]
    seg_index, (segment, _) = next((i,s) for i,s in enumerate(segments) if s[1]==b'__DATA_CONST')
    vm, vmsize, fileoff, filesize = struct.unpack_from('<QQQQ',data,segment+24)
    last = max(s['offset']+s['size'] for s in m.sections if s['segment']=='__DATA_CONST')
    slot = (last+7) & ~7
    assert slot+8 <= fileoff+filesize and data[slot:slot+8] == bytes(8)
    first = min(s['offset'] for s in m.sections if s['offset']>0)
    redirects = []
    if sdl_path:
        exports = {s['name'] for s in MachO(sdl_path).symbols() if s['type']&1 and 'library' not in s}
        for s in m.symbols():
            if s['name'].startswith('_SDL_') and s['name'] in exports and 'library' not in s:
                section = m.sections[s['section']-1]
                if section['name']=='__text':
                    redirects.append(s)
    stub = (first-16*(1+len(redirects))) & ~15
    assert stub >= 32+struct.unpack_from('<I',data,20)[0]
    assert data[stub:stub+16*(1+len(redirects))] == bytes(16*(1+len(redirects)))
    text = next(p for p,name in segments if name==b'__TEXT')
    base = struct.unpack_from('<Q',data,text+24)[0]
    def trampoline(at, pointer_vm):
        pc = base+at
        pages = ((pointer_vm & ~4095) - (pc & ~4095)) >> 12
        assert -(1<<20) <= pages < (1<<20)
        adrp = 0x90000010 | ((pages & 3)<<29) | (((pages>>2)&0x7ffff)<<5)
        ldr = 0xf9400210 | (((pointer_vm & 4095)//8)<<10)
        data[at:at+16] = struct.pack('<4I',adrp,ldr,0xd61f0200,0xd503201f)
    trampoline(stub, vm+slot-fileoff)
    dyld = next(p for c,p,n in m.commands if c==0x80000022)
    bindoff, bindsize = struct.unpack_from('<II',data,dyld+16)
    oldbind = data[bindoff:bindoff+bindsize]
    # Ordinal 1 is P00.dylib, which reexports ProbeShim. Reset bind state explicitly.
    def binding_for(symbol,index,offset):
        return b'\x11\x40'+symbol.encode()+b'\0\x51\x60\0'+bytes([0x70|index])+uleb(offset)+b'\x90'
    extra = binding_for('_ProbeMain',seg_index,slot-fileoff)
    data_index,(data_segment,_) = next((i,s) for i,s in enumerate(segments) if s[1]==b'__DATA')
    data_vm,data_vmsize,data_off,data_size = struct.unpack_from('<QQQQ',data,data_segment+24)
    bss_end = max(s['address']+s['size'] for s in m.sections if s['segment']=='__DATA')
    bss_slot = (bss_end+7)&~7
    const_slot = slot+8
    # Each redirect needs one 8-byte dyld-bound pointer. SDL3 exports far more
    # functions than SDL2 did, so the stock padding may not suffice. Grow the
    # zero-fill tail of __DATA by whole 16 KiB pages and slide __LINKEDIT's VM
    # address by the same amount; no file contents move.
    const_free = max(0,(fileoff+filesize-const_slot)//8)
    bss_free = max(0,(data_vm+data_vmsize-bss_slot)//8)
    shortfall = len(redirects)-const_free-bss_free
    if shortfall > 0:
        grow = ((shortfall*8)+16383)&~16383
        struct.pack_into('<Q',data,data_segment+32,data_vmsize+grow)
        data_vmsize += grow
        linkedit_segment = next(p for p,name in segments if name==b'__LINKEDIT')
        link_vm = struct.unpack_from('<Q',data,linkedit_segment+24)[0]
        assert link_vm == data_vm+data_vmsize-grow, '__LINKEDIT does not directly follow __DATA'
        struct.pack_into('<Q',data,linkedit_segment+24,link_vm+grow)
    changes = []
    for i,symbol in enumerate(redirects):
        at = stub+16*(i+1)
        if const_slot+8 <= fileoff+filesize:
            assert data[const_slot:const_slot+8]==bytes(8)
            pointer_vm = vm+const_slot-fileoff
            extra += binding_for(symbol['name'],seg_index,const_slot-fileoff)
            const_slot += 8
        else:
            assert bss_slot>=data_vm+data_size and bss_slot+8<=data_vm+data_vmsize, 'Insufficient zero-fill padding'
            pointer_vm = bss_slot
            extra += binding_for(symbol['name'],data_index,bss_slot-data_vm)
            bss_slot += 8
        trampoline(at,pointer_vm)
        offset = symbol['address']-base
        displacement = base+at-symbol['address']
        assert displacement % 4 == 0 and -(1<<27)<=displacement<(1<<27)
        before = data[offset:offset+4].hex()
        struct.pack_into('<I',data,offset,0x14000000|((displacement>>2)&0x3ffffff))
        changes.append(dict(name=symbol['name'],offset=offset,original_bytes=before,trampoline=at))
    # Factorio 2.1.24 needs no instruction patches beyond the SDL redirects:
    # SDLWindow::logDisplays() now uses only public SDL3 display APIs, and the
    # old simulator-only Metal gate patch no longer matches this build.
    platform_patches = []
    extra += b'\0'
    binding = oldbind[:bind_end(oldbind)] + extra
    newoff = (len(data)+7)&~7
    data += bytes(newoff-len(data)) + binding
    struct.pack_into('<II',data,dyld+16,newoff,len(binding))
    linkedit = next(p for p,name in segments if name==b'__LINKEDIT')
    leoff = struct.unpack_from('<Q',data,linkedit+40)[0]
    struct.pack_into('<Q',data,linkedit+48,len(data)-leoff)
    struct.pack_into('<Q',data,linkedit+32,(len(data)-leoff+16383)&~16383)
    main = next(p for c,p,n in m.commands if c==0x80000028)
    original = struct.unpack_from('<Q',data,main+8)[0]
    struct.pack_into('<Q',data,main+8,stub)
    path.write_bytes(data)
    return dict(original_entry_offset=original,entry_stub_offset=stub,pointer_slot_offset=slot,bind_stream_offset=newoff,sdl_redirects=changes,platform_patches=platform_patches)
