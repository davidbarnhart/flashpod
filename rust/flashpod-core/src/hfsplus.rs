//! An empty HFS+ volume inside an HFS wrapper, for a Mac-formatted iPod
//! card (#104). The layout is emupod's hfsplus_gen.wrapped() with no files,
//! the card that booted and played on a 1st-gen with stock firmware
//! (2026-10-27): Apple TN1150's volume header and its copy, the allocation
//! bitmap, an empty extents-overflow B-tree and a catalog B-tree holding
//! the root folder and its thread, embedded in a minimal HFS 'BD' master
//! directory block whose drEmbedExtent names the whole HFS+ volume.
//!
//! Two differences from the generator, both for real card sizes:
//! - the wrapper's allocation block grows (4 KiB, 8 KiB, ...) until its
//!   block count fits the MDB's 16-bit fields, and its first allocation
//!   block moves past the wrapper bitmap when that bitmap would reach it;
//! - only metadata is written (the volume is never built in memory), and
//!   the catalog is preallocated in proportion to the volume, so a full
//!   library fits in one extent.
//! Up to about 218 MB both give exactly the generator's bytes.

use crate::block::BlockDevice;
use std::io;

const BLOCK: u64 = 4096; // HFS+ allocation block and B-tree node size
const NODE: usize = 4096;
const SECTOR: u64 = 512;
pub const MAC_EPOCH: u64 = 2_082_844_800;
const ROOT_PARENT: u32 = 1;
const ROOT_ID: u32 = 2;
const FIRST_ID: u32 = 16;

/// What format_wrapped laid out.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Geometry {
    /// Wrapper allocation block size in bytes, and how many there are.
    pub wrapper_block: u64,
    pub wrapper_blocks: u64,
    /// The HFS+ volume's byte offset in the partition, block size and count.
    pub volume_offset: u64,
    pub block_size: u64,
    pub total_blocks: u64,
    pub free_blocks: u64,
    pub catalog_nodes: u64,
}

fn be16(b: &mut [u8], off: usize, v: u16) {
    b[off..off + 2].copy_from_slice(&v.to_be_bytes());
}
fn be32(b: &mut [u8], off: usize, v: u32) {
    b[off..off + 4].copy_from_slice(&v.to_be_bytes());
}

fn uni(name: &str) -> Vec<u8> {
    let u: Vec<u16> = name.encode_utf16().collect();
    let mut out = (u.len() as u16).to_be_bytes().to_vec();
    for c in u {
        out.extend_from_slice(&c.to_be_bytes());
    }
    out
}

fn cat_key(parent: u32, name: &str) -> Vec<u8> {
    let mut body = parent.to_be_bytes().to_vec();
    body.extend(uni(name));
    let mut k = (body.len() as u16).to_be_bytes().to_vec();
    k.extend(body);
    k
}

/// A fork: logical size, clump 0, total blocks, one extent (or none).
fn fork(size: u64, start: u64, count: u64) -> [u8; 80] {
    let mut f = [0u8; 80];
    f[0..8].copy_from_slice(&size.to_be_bytes());
    be32(&mut f, 12, count as u32);
    if count != 0 {
        be32(&mut f, 16, start as u32);
        be32(&mut f, 20, count as u32);
    }
    f
}

fn folder_record(cnid: u32, valence: u32, now: u32) -> Vec<u8> {
    let mut r = vec![0u8; 88];
    be16(&mut r, 0, 1);
    be32(&mut r, 4, valence);
    be32(&mut r, 8, cnid);
    for k in 0..4 {
        be32(&mut r, 12 + 4 * k, now);
    }
    be16(&mut r, 32 + 10, 0o40755);
    r
}

fn thread_record(kind: u16, parent: u32, name: &str) -> Vec<u8> {
    let mut r = vec![0u8; 8];
    be16(&mut r, 0, kind);
    be32(&mut r, 4, parent);
    r.extend(uni(name));
    r
}

/// One B-tree node: descriptor, records (each padded to even length), and
/// the offsets table counting back from the node's end.
fn node(kind: i8, height: u8, records: &[Vec<u8>], flink: u32, blink: u32) -> Vec<u8> {
    let mut out = vec![0u8; NODE];
    be32(&mut out, 0, flink);
    be32(&mut out, 4, blink);
    out[8] = kind as u8;
    out[9] = height;
    be16(&mut out, 10, records.len() as u16);
    let mut off = 14;
    let mut offs = Vec::new();
    for r in records {
        offs.push(off);
        out[off..off + r.len()].copy_from_slice(r);
        off += r.len() + r.len() % 2;
    }
    offs.push(off);
    for (k, o) in offs.iter().enumerate() {
        be16(&mut out, NODE - 2 * (k + 1), *o as u16);
    }
    out
}

/// The header node (node 0) of a B-tree with `used` nodes in use.
#[allow(clippy::too_many_arguments)]
fn header_node(depth: u16, root: u32, leaf_records: u32, first_leaf: u32, last_leaf: u32,
               total: u32, free: u32, max_key: u16, cmp: u8, attrs: u32, used: u32) -> Vec<u8> {
    let mut hdr = vec![0u8; 106];
    be16(&mut hdr, 0, depth);
    be32(&mut hdr, 2, root);
    be32(&mut hdr, 6, leaf_records);
    be32(&mut hdr, 10, first_leaf);
    be32(&mut hdr, 14, last_leaf);
    be16(&mut hdr, 18, NODE as u16);
    be16(&mut hdr, 20, max_key);
    be32(&mut hdr, 22, total);
    be32(&mut hdr, 26, free);
    be32(&mut hdr, 32, NODE as u32); // clumpSize
    hdr[36] = 0; // btreeType
    hdr[37] = cmp;
    be32(&mut hdr, 38, attrs);
    let mut map = vec![0u8; NODE - 14 - 106 - 128 - 8];
    for n in 0..used as usize {
        map[n / 8] |= 0x80 >> (n % 8);
    }
    node(1, 0, &[hdr, vec![0u8; 128], map], 0, 0)
}

/// The volume header (and its copy at the end of the volume).
#[allow(clippy::too_many_arguments)]
fn volume_header(now: u32, total: u64, free: u64, next_alloc: u64, bitmap: (u64, u64, u64),
                 extents: (u64, u64, u64), catalog: (u64, u64, u64)) -> [u8; 512] {
    let mut vh = [0u8; 512];
    vh[0..2].copy_from_slice(b"H+");
    be16(&mut vh, 2, 4);
    be32(&mut vh, 4, 1 << 8); // kHFSVolumeUnmountedBit
    vh[8..12].copy_from_slice(b"10.0");
    for off in [16, 20, 28] {
        be32(&mut vh, off, now); // created, modified, checked (backup 0)
    }
    // fileCount, folderCount 0
    be32(&mut vh, 40, BLOCK as u32);
    be32(&mut vh, 44, total as u32);
    be32(&mut vh, 48, free as u32);
    be32(&mut vh, 52, next_alloc as u32);
    be32(&mut vh, 56, BLOCK as u32);
    be32(&mut vh, 60, BLOCK as u32);
    be32(&mut vh, 64, FIRST_ID);
    be32(&mut vh, 68, 1); // writeCount
    vh[72..80].copy_from_slice(&1u64.to_be_bytes()); // encodingsBitmap: MacRoman
    vh[112..192].copy_from_slice(&fork(bitmap.0, bitmap.1, bitmap.2));
    vh[192..272].copy_from_slice(&fork(extents.0, extents.1, extents.2));
    vh[272..352].copy_from_slice(&fork(catalog.0, catalog.1, catalog.2));
    vh
}

/// Catalog nodes to preallocate: the generator's 64 on a small card, then
/// one node (4 KiB) per MiB of volume, at most 8192 (32 MiB, room for tens
/// of thousands of tracks in one contiguous extent).
pub fn catalog_nodes(volume_bytes: u64) -> u64 {
    (volume_bytes / (BLOCK * 1024)).clamp(64, 8192)
}

/// The wrapper for a partition of `size` bytes: (allocation block size,
/// first allocation block in sectors, block count). The MDB sits at 1024,
/// its bitmap from sector 3, the allocation blocks from sector 16 (later if
/// the bitmap reaches it), the alternate MDB 1024 bytes before the end; the
/// block size doubles from 4 KiB until the count fits the MDB's u16 fields.
pub fn wrapper_geometry(size: u64) -> (u64, u64, u64) {
    let mut wblock = BLOCK;
    loop {
        let mut al_st = 16u64;
        let mut nblk = size.saturating_sub(al_st * SECTOR + 1024) / wblock;
        let bm_sectors = nblk.div_ceil(8).div_ceil(SECTOR);
        if 3 + bm_sectors > al_st {
            al_st = 3 + bm_sectors;
            nblk = size.saturating_sub(al_st * SECTOR + 1024) / wblock;
        }
        if nblk <= 0xFFFF {
            return (wblock, al_st, nblk);
        }
        wblock *= 2;
    }
}

/// Write an empty HFS+ volume named `name`, wrapped, over `sectors` sectors
/// of `dev` starting at sector `part_start`. `now` is the volume's dates
/// in seconds since 1904 (the caller's clock; fixed in tests).
pub fn format_wrapped(dev: &mut dyn BlockDevice, part_start: u64, sectors: u64, name: &str, now: u32)
    -> io::Result<Geometry>
{
    let size = sectors * SECTOR;
    let base = part_start * SECTOR;
    let inval = |m: &str| io::Error::new(io::ErrorKind::InvalidInput, m.to_string());
    let (wblock, al_st, nblk) = wrapper_geometry(size);
    let vol_bytes = nblk * wblock;
    let total = vol_bytes / BLOCK;
    let bitmap_blocks = total.div_ceil(8 * BLOCK);
    let ext_nodes = 4u64;
    let cat_nodes = catalog_nodes(vol_bytes);
    let alloc_at = 1;
    let ext_at = alloc_at + bitmap_blocks;
    let cat_at = ext_at + ext_nodes * NODE as u64 / BLOCK;
    let next = cat_at + cat_nodes * NODE as u64 / BLOCK;
    if total < 8 || next >= total - 1 {
        return Err(inval("partition too small for an HFS+ volume"));
    }
    let used = next + 1; // the metadata, plus the last block (alternate header)
    let free = total - used;
    let vstart = base + al_st * SECTOR;

    // wrapper MDB
    let mut mdb = [0u8; 512];
    mdb[0..2].copy_from_slice(b"BD");
    be32(&mut mdb, 2, now);
    be32(&mut mdb, 6, now);
    be16(&mut mdb, 10, 1 << 8);
    be16(&mut mdb, 12, 0);
    be16(&mut mdb, 14, 3); // drVBMSt
    be16(&mut mdb, 16, 0);
    be16(&mut mdb, 18, nblk as u16); // drNmAlBlks
    be32(&mut mdb, 20, wblock as u32); // drAlBlkSiz
    be32(&mut mdb, 24, wblock as u32); // drClpSiz
    be16(&mut mdb, 28, al_st as u16); // drAlBlSt
    be32(&mut mdb, 30, FIRST_ID); // drNxtCNID
    be16(&mut mdb, 0x22, 0); // drFreeBks
    let vn = &name.as_bytes()[..name.len().min(27)];
    mdb[0x24] = vn.len() as u8;
    mdb[0x25..0x25 + vn.len()].copy_from_slice(vn);
    mdb[0x7c..0x7e].copy_from_slice(b"H+");
    be16(&mut mdb, 0x7e, 0);
    be16(&mut mdb, 0x80, nblk as u16);
    // sectors 0..al_st: boot blocks zero, the MDB, the wrapper bitmap (every
    // block the embedded volume's)
    let mut head = vec![0u8; (al_st * SECTOR) as usize];
    head[1024..1536].copy_from_slice(&mdb);
    for b in 0..nblk as usize {
        head[3 * SECTOR as usize + b / 8] |= 0x80 >> (b % 8);
    }
    dev.write_at(base, &head)?;
    dev.write_at(base + size - 1024, &mdb)?;
    dev.write_at(base + size - 512, &[0u8; 512])?;

    // the HFS+ volume: boot blocks, header at 1024 (block 0 is all of it)
    let vh = volume_header(now, total, free, next,
                           (bitmap_blocks * BLOCK, alloc_at, bitmap_blocks),
                           (ext_nodes * NODE as u64, ext_at, ext_nodes),
                           (cat_nodes * NODE as u64, cat_at, cat_nodes));
    let mut block0 = vec![0u8; BLOCK as usize];
    block0[1024..1536].copy_from_slice(&vh);
    dev.write_at(vstart, &block0)?;
    // allocation bitmap: blocks 0..next and the last one, a chunk at a time
    let chunk = 1 << 20;
    let bm_bytes = bitmap_blocks * BLOCK;
    let mut off = 0u64;
    while off < bm_bytes {
        let n = chunk.min(bm_bytes - off) as usize;
        let mut buf = vec![0u8; n];
        for (i, byte) in buf.iter_mut().enumerate() {
            let first = (off + i as u64) * 8;
            for bit in 0..8u64 {
                let b = first + bit;
                if b < used - 1 || b == total - 1 {
                    *byte |= 0x80 >> bit;
                }
            }
        }
        dev.write_at(vstart + alloc_at * BLOCK + off, &buf)?;
        off += n as u64;
    }
    // extents-overflow B-tree: the header node, the rest free
    let mut ext = header_node(0, 0, 0, 0, 0, ext_nodes as u32, ext_nodes as u32 - 1, 10, 0, 0x2, 1);
    ext.resize(ext_nodes as usize * NODE, 0);
    dev.write_at(vstart + ext_at * BLOCK, &ext)?;
    // catalog B-tree: the header node, one leaf with the root folder and its
    // thread (sorted by parent, then name), the rest free
    let recs = [
        [cat_key(ROOT_PARENT, name), folder_record(ROOT_ID, 0, now)].concat(),
        [cat_key(ROOT_ID, ""), thread_record(3, ROOT_PARENT, name)].concat(),
    ];
    let mut cat = header_node(1, 1, 2, 1, 1, cat_nodes as u32, cat_nodes as u32 - 2, 516, 0xCF, 0x6, 2);
    cat.extend(node(-1, 1, &recs, 0, 0));
    let zeros = vec![0u8; chunk as usize];
    dev.write_at(vstart + cat_at * BLOCK, &cat)?;
    let mut off = cat.len() as u64;
    let cat_bytes = cat_nodes * NODE as u64;
    while off < cat_bytes {
        let n = (chunk as u64).min(cat_bytes - off) as usize;
        dev.write_at(vstart + cat_at * BLOCK + off, &zeros[..n])?;
        off += n as u64;
    }
    // the alternate volume header, 1024 bytes before the volume's end
    let mut tail = vec![0u8; 1024];
    tail[..512].copy_from_slice(&vh);
    dev.write_at(vstart + vol_bytes - 1024, &tail)?;
    dev.flush()?;
    Ok(Geometry {
        wrapper_block: wblock,
        wrapper_blocks: nblk,
        volume_offset: al_st * SECTOR,
        block_size: BLOCK,
        total_blocks: total,
        free_blocks: free,
        catalog_nodes: cat_nodes,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::block::MemDevice;

    #[test]
    fn small_wrapper_keeps_4k_blocks() {
        let mut d = MemDevice { data: vec![0; 64 << 20] };
        let g = format_wrapped(&mut d, 0, (64 << 20) / 512, "iPod", 0).unwrap();
        assert_eq!((g.wrapper_block, g.volume_offset), (4096, 16 * 512));
        assert_eq!(&d.data[1024..1026], b"BD");
        assert_eq!(&d.data[8192 + 1024..8192 + 1026], b"H+");
    }

    #[test]
    fn wrapper_geometry_scales() {
        // 133 MB (the hardware-tested card's size): 4 KiB blocks, sector 16
        let (w, al, n) = wrapper_geometry(133 << 20);
        assert_eq!((w, al), (4096, 16));
        assert!(n * w <= (133 << 20) - 16 * 512 - 1024);
        // past ~218 MB the bitmap pushes the first block out; past 256 MB the
        // block doubles; every size keeps the count in 16 bits and the
        // bitmap clear of the blocks
        for mb in [200u64, 230, 300, 1000, 2000, 8000, 64000, 131072] {
            let size = mb << 20;
            let (w, al, n) = wrapper_geometry(size);
            assert!(n <= 0xFFFF, "{} MB", mb);
            assert!(3 + n.div_ceil(8).div_ceil(512) <= al, "{} MB", mb);
            assert!(al * 512 + n * w + 1024 <= size, "{} MB", mb);
            assert!(n * w * 100 / size >= 99, "{} MB wastes over 1%", mb);
        }
        assert_eq!(wrapper_geometry(128u64 << 30).0, 2 << 20);
    }
}
