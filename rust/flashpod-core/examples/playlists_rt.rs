//! Read an iTunesDB file, print its playlists as name:ids lines, add a
//! playlist "Rust Made" of its first two tracks, write OUT.
//!   playlists_rt IN_DB OUT_DB
fn main() {
    let a: Vec<String> = std::env::args().collect();
    let mut ent = flashpod_core::entropy::Entropy::new(5, 0);
    let mut lib = flashpod_core::itunesdb::parse_bytes_with(&std::fs::read(&a[1]).unwrap(), &mut ent).unwrap();
    for p in &lib.playlists {
        println!("{}:{}", p.name, p.ids.iter().map(|i| i.to_string()).collect::<Vec<_>>().join(","));
    }
    let ids: Vec<u32> = lib.tracks.iter().take(2).map(|t| t.id).collect();
    flashpod_core::playlist::create(&mut lib, "Rust Made", &ids).unwrap();
    std::fs::write(&a[2], flashpod_core::itunesdb::serialize(&lib, &mut ent)).unwrap();
}
