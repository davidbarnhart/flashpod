//! Editing a library's user playlists (#106): create, add to, take from,
//! delete. Pure edits of a Library; the caller loads and saves it. The
//! checks are scripts/add_playlists.py's (claude/playlist-tool): no empty
//! name, no track id the library lacks, no track twice in one playlist;
//! names are also unique, case-insensitively, so commands can find one.

use crate::error::{Error, ErrorKind};
use crate::itunesdb::{Library, Playlist};

fn find(lib: &Library, name: &str) -> Option<usize> {
    let want = name.trim().to_lowercase();
    lib.playlists.iter().position(|p| p.name.trim().to_lowercase() == want)
}

fn no_such(name: &str) -> Error {
    Error::new(ErrorKind::Error, format!("no playlist named {:?}", name)).detail("reason", "no_such_playlist")
}

fn check_ids(lib: &Library, ids: &[u32]) -> Result<(), Error> {
    let have: std::collections::HashSet<u32> = lib.tracks.iter().map(|t| t.id).collect();
    let unknown: Vec<u32> = ids.iter().copied().filter(|i| !have.contains(i)).collect();
    if !unknown.is_empty() {
        return Err(Error::new(ErrorKind::NoSuchTrack, format!("no track with id {:?}", unknown))
            .detail("ids", unknown));
    }
    Ok(())
}

/// A new playlist `name` holding `ids` (may be empty) in that order.
pub fn create(lib: &mut Library, name: &str, ids: &[u32]) -> Result<(), Error> {
    if name.trim().is_empty() {
        return Err(Error::new(ErrorKind::Error, "a playlist needs a name").detail("reason", "empty_name"));
    }
    if find(lib, name).is_some() {
        return Err(Error::new(ErrorKind::Error, format!("there is already a playlist named {:?}", name))
            .detail("reason", "exists"));
    }
    check_ids(lib, ids)?;
    let mut seen = std::collections::HashSet::new();
    if let Some(d) = ids.iter().find(|i| !seen.insert(**i)) {
        return Err(Error::new(ErrorKind::Error, format!("track {} is listed twice", d)).detail("reason", "duplicate"));
    }
    lib.playlists.push(Playlist { name: name.trim().to_string(), ids: ids.to_vec() });
    Ok(())
}

/// Append `ids` to playlist `name`, skipping ones it already holds; the
/// number appended.
pub fn add(lib: &mut Library, name: &str, ids: &[u32]) -> Result<usize, Error> {
    check_ids(lib, ids)?;
    let i = find(lib, name).ok_or_else(|| no_such(name))?;
    let p = &mut lib.playlists[i];
    let before = p.ids.len();
    for &id in ids {
        if !p.ids.contains(&id) {
            p.ids.push(id);
        }
    }
    Ok(p.ids.len() - before)
}

/// Take `ids` out of playlist `name` (the tracks stay on the iPod); the
/// number taken out.
pub fn take(lib: &mut Library, name: &str, ids: &[u32]) -> Result<usize, Error> {
    let i = find(lib, name).ok_or_else(|| no_such(name))?;
    let p = &mut lib.playlists[i];
    let before = p.ids.len();
    p.ids.retain(|id| !ids.contains(id));
    Ok(before - p.ids.len())
}

/// Removed tracks leave every playlist (a playlist may end up empty).
pub fn forget_tracks(lib: &mut Library, ids: &[u32]) {
    for p in &mut lib.playlists {
        p.ids.retain(|id| !ids.contains(id));
    }
}

/// Delete playlist `name` (its tracks stay on the iPod).
pub fn delete(lib: &mut Library, name: &str) -> Result<Playlist, Error> {
    let i = find(lib, name).ok_or_else(|| no_such(name))?;
    Ok(lib.playlists.remove(i))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::entropy::Entropy;
    use crate::itunesdb::{parse_bytes_with, serialize, Track};

    fn lib() -> Library {
        let mut ent = Entropy::new(7, 0);
        let mut l = Library::new("iPod", &mut ent);
        for id in [52, 53, 54, 55] {
            l.tracks.push(Track { id, title: Some(format!("t{}", id)), location: Some(format!(":iPod_Control:Music:F00:fp{}.mp3", id)), ..Default::default() });
        }
        l
    }

    #[test]
    fn edits_and_refusals() {
        let mut l = lib();
        create(&mut l, "Road Trip", &[52, 53]).unwrap();
        create(&mut l, "Empty", &[]).unwrap();
        assert_eq!(create(&mut l, "road trip", &[54]).unwrap_err().details["reason"], "exists");
        assert_eq!(create(&mut l, "  ", &[54]).unwrap_err().details["reason"], "empty_name");
        assert_eq!(create(&mut l, "X", &[54, 54]).unwrap_err().details["reason"], "duplicate");
        assert_eq!(create(&mut l, "X", &[99]).unwrap_err().kind, ErrorKind::NoSuchTrack);
        assert_eq!(add(&mut l, "ROAD TRIP", &[53, 55]).unwrap(), 1);
        assert_eq!(take(&mut l, "Road Trip", &[52]).unwrap(), 1);
        assert_eq!(l.playlists[0].ids, [53, 55]);
        assert_eq!(delete(&mut l, "Empty").unwrap().name, "Empty");
        assert_eq!(delete(&mut l, "Empty").unwrap_err().details["reason"], "no_such_playlist");
    }

    #[test]
    fn removed_tracks_leave_every_playlist() {
        let mut l = lib();
        create(&mut l, "A", &[52, 53, 54]).unwrap();
        create(&mut l, "B", &[53]).unwrap();
        forget_tracks(&mut l, &[53, 99]);
        assert_eq!((l.playlists[0].ids.clone(), l.playlists[1].ids.clone()), (vec![52, 54], vec![]));
    }

    #[test]
    fn playlists_survive_a_save_and_load() {
        let mut l = lib();
        create(&mut l, "Road Trip", &[54, 52]).unwrap();
        create(&mut l, "Solo", &[55]).unwrap();
        let mut ent = Entropy::new(7, 0);
        let bytes = serialize(&l, &mut ent);
        let back = parse_bytes_with(&bytes, &mut Entropy::new(8, 0)).unwrap();
        assert_eq!(back.playlists, l.playlists);
        assert_eq!(back.tracks.len(), 4);
        assert_eq!(back.name, "iPod");
    }

    #[test]
    fn no_playlists_writes_the_same_bytes_as_before() {
        // mhlp count 1 and nothing after the master: the bytes Python writes
        let l = lib();
        let bytes = serialize(&l, &mut Entropy::new(7, 0));
        let mhlp = bytes.windows(4).position(|w| w == b"mhlp").unwrap();
        assert_eq!(u32::from_le_bytes(bytes[mhlp + 8..mhlp + 12].try_into().unwrap()), 1);
        assert_eq!(bytes.windows(4).filter(|w| *w == b"mhyp").count(), 1);
    }
}
