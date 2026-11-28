//! Tests of what main.rs itself decides: where a root joins a route, the umask a walk runs under, and which
//! folder the atlas is taken from.

use super::RootUmask;

#[test]
fn a_root_partway_along_a_route_walks_the_rest_of_it() {
    let atlas = super::Atlas::load(&std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("..")).unwrap();
    let el = &atlas.adapters["el"];
    let (_, c5) = atlas.route("centos5-to-alma10").unwrap();
    let (_, c7) = atlas.route("centos7-to-alma10").unwrap();
    let at = |r, rel: &str| super::joins_at(el, r, Some(&rel.to_string()));
    assert_eq!(super::joins_at(el, c5, None), Ok(None));
    assert_eq!(at(c5, "centos5"), Ok(None));
    assert_eq!(at(c5, "centos6"), Ok(Some("centos6".into())));
    assert_eq!(at(c5, "alma9"), Ok(Some("alma9".into())));
    assert_eq!(at(c7, "sl7"), Ok(None));
    for (r, rel) in [(c7, "centos6"), (c5, "alma10"), (c5, "centos8")] {
        let e = at(r, rel).unwrap_err();
        assert!(e.contains("doesn't pass through"), "{e}");
    }
}

fn mask_now() -> libc::mode_t {
    // SAFETY: reads the mask by setting it and putting it straight back.
    unsafe {
        let m = libc::umask(0o077);
        libc::umask(m);
        m
    }
}

/// The only test that touches the process umask: 022 while the guard lives, the caller's mask after, also when
/// the walk panics.
#[test]
fn root_umask_is_022_inside_and_restores_the_callers_mask() {
    // SAFETY: as above.
    let saved = unsafe { libc::umask(0o077) };
    let g = RootUmask::set();
    assert_eq!(mask_now(), 0o022);
    drop(g);
    assert_eq!(mask_now(), 0o077);
    let r = std::panic::catch_unwind(|| {
        let _g = RootUmask::set();
        assert_eq!(mask_now(), 0o022);
        panic!("walk failed");
    });
    assert!(r.is_err());
    assert_eq!(mask_now(), 0o077);
    // SAFETY: as above.
    unsafe { libc::umask(saved) };
}
#[test]
fn the_atlas_is_taken_from_a_folder_that_holds_one() {
    use super::{fsx, pick_atlas, Atlas};
    // a folder that only looks like one (somebody else's `adapters/`) is not chosen over the atlas beside the
    // binary: the release's verdict would be lost without a word
    let root = std::env::temp_dir().join(format!("wend-pick-{}", std::process::id()));
    let _ = fsx::remove_dir_all(&root);
    let (project, release) = (root.join("project"), root.join("release"));
    std::fs::create_dir_all(project.join("adapters")).unwrap();
    std::fs::create_dir_all(&release).unwrap();
    let wend = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("..");
    let holds = |dir: &std::path::Path| Atlas::load(dir).is_ok();
    assert!(holds(&wend) && !holds(&project) && !holds(&release));
    assert_eq!(pick_atlas(&project, Some(&wend), &holds), wend);
    // where it was started wins when it holds one, and is what an error is about when neither does
    assert_eq!(pick_atlas(&wend, Some(&project), &holds), wend);
    assert_eq!(pick_atlas(&project, Some(&release), &holds), project);
    assert_eq!(pick_atlas(&project, None, &holds), project);
    // the test default_atlas used before this: a folder named adapters was enough
    let named = |dir: &std::path::Path| dir.join("adapters").is_dir();
    assert_eq!(pick_atlas(&project, Some(&wend), &named), project);
    let _ = fsx::remove_dir_all(&root);
}
