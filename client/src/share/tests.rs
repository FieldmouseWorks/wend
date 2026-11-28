//! Tests of the report: what names a machine, what is scrubbed from it, and which routes it is judged against.

use super::*;

/// The report from facts already gathered, as `prepare` makes it: (summary, full report, title, route name).
fn build(
    atlas: Option<&Atlas>,
    route: Option<&str>,
    sys: &System,
    kernel: &str,
    facts: &Value,
    words: &[String],
) -> Result<(String, String, String, String), String> {
    Ok(render(
        sys,
        kernel,
        facts,
        &judge(atlas, route, sys, facts)?,
        words,
        atlas.is_some(),
    ))
}

fn tree(files: &[(&str, &str)]) -> std::path::PathBuf {
    static N: std::sync::atomic::AtomicUsize = std::sync::atomic::AtomicUsize::new(0);
    let n = N.fetch_add(1, std::sync::atomic::Ordering::SeqCst);
    let d = std::env::temp_dir().join(format!("wend-share-{}-{n}", std::process::id()));
    let _ = crate::fsx::remove_dir_all(&d);
    for (p, c) in files {
        let f = d.join(p);
        fs::create_dir_all(f.parent().unwrap()).unwrap();
        fs::write(f, c).unwrap();
    }
    d
}

#[test]
fn names_the_release() {
    let c7 = tree(&[("etc/redhat-release", "CentOS Linux release 7.9.2009 (Core)\n")]);
    let s = system(&c7);
    assert_eq!(s.release, Some(("el".into(), "centos7".into())));
    assert_eq!(s.pretty, "CentOS Linux release 7.9.2009 (Core)");
    let c5 = tree(&[("etc/redhat-release", "CentOS release 5.11 (Final)\n")]);
    assert_eq!(system(&c5).release, Some(("el".into(), "centos5".into())));
    let alma9 = tree(&[("etc/redhat-release", "AlmaLinux release 9.4 (Seafoam Ocelot)\n")]);
    assert_eq!(system(&alma9).release, Some(("el".into(), "alma9".into())));
    let stream = tree(&[("etc/redhat-release", "CentOS Stream release 8\n")]);
    assert_eq!(system(&stream).release, None);
    let trusty = tree(&[
        (
            "etc/lsb-release",
            "DISTRIB_ID=Ubuntu\nDISTRIB_CODENAME=trusty\nDISTRIB_DESCRIPTION=\"Ubuntu 14.04.6 LTS\"\n",
        ),
        ("etc/debian_version", "jessie/sid\n"),
    ]);
    let s = system(&trusty);
    assert_eq!(s.release, Some(("ubuntu".into(), "trusty".into())));
    assert_eq!(s.pretty, "Ubuntu 14.04.6 LTS");
    let etch = tree(&[("etc/debian_version", "4.0\n")]);
    assert_eq!(system(&etch).release, Some(("debian".into(), "etch".into())));
}

#[test]
fn scrubs_what_identifies_the_machine() {
    let root = tree(&[
        ("etc/passwd", "root:x:0:0::/root:/bin/bash\nalice:x:1000:1000::/home/alice:/bin/bash\ncarol:x:500:500::/home/carol:/bin/bash\nnobody:x:65534:65534::/:/sbin/nologin\n"),
        ("etc/hostname", "web-01.acme.example\n"),
    ]);
    let words = local_words(&root, "web-01.acme.example", &["billing".into()]);
    assert!(words.contains(&"web-01".to_string()) && words.contains(&"alice".to_string()));
    assert!(
        words.contains(&"carol".to_string()),
        "EL5's first login account is uid 500"
    );
    assert!(!words.contains(&"nobody".to_string()) && !words.contains(&"root".to_string()));
    let s = scrub(
        "/dev/mapper/centos_web--01-root on / ; /srv/billing/app.py ; /home/bob/x.py ; /var/spool/cron/alice ; \
         10.1.2.3 ; fe80::5054:ff:fe12:3456 ; 2001:db8::1 ; ops@acme.example ; 52:54:00:12:34:56 ; password=hunter2 ; web-01.acme.example ; ALICE",
        &words,
    );
    for gone in [
        "web--01", "web-01", "billing", "bob", "alice", "ALICE", "10.1.2.3", "fe80", "db8", "ops@", "52:54", "hunter2",
    ] {
        assert!(!s.contains(gone), "{gone} survived: {s}");
    }
    assert!(s.contains("/dev/mapper/centos_<local>-root"), "{s}");
    assert!(s.contains("/var/spool/cron/<user>"), "{s}");
}

fn sys(fam: &str, rel: &str) -> System {
    System {
        pretty: format!("{fam} {rel}"),
        release: Some((fam.into(), rel.into())),
    }
}

#[test]
fn a_machine_partway_along_a_route_takes_the_rest_of_it() {
    let a = Atlas::load(&Path::new(env!("CARGO_MANIFEST_DIR")).join("..")).unwrap();
    let f = serde_json::json!({"schema": "wend.hardware/4", "arch": "x86_64", "userland_bits": 64,
        "cpu": {"model": "test", "features": ["cx16", "lahf_lm", "popcnt", "sse4_1", "sse4_2", "ssse3", "sse3"],
                "hypervisor": true},
        "boot": "bios", "ram_mib": 4096, "free_mib": 40000,
        "boot_dir": {"separate": false, "total_mib": 40000, "free_mib": 40000, "kernels": []},
        "blacklisted": [], "pci": [], "runtimes": [], "python2_modules": [], "native": [], "kernel_owner": "kernel",
        "repo_packages": [], "mounts": []});
    let report =
        |fam: &str, rel: &str, route: Option<&str>| build(Some(&a), route, &sys(fam, rel), "k", &f, &[]).unwrap();
    // CentOS 6: no route starts there, the CentOS 4 and CentOS 5 ones pass through
    let (summary, _, _, names) = report("el", "centos6", None);
    assert_eq!(names, "centos4-to-alma10, centos5-to-alma10");
    assert!(
        summary.contains("joins this route at its centos6 rung, so it walks only centos7, alma9, alma10"),
        "{summary}"
    );
    assert!(summary.contains("walked up from centos5"), "{summary}");
    // the same, asked for by name
    let (summary, _, _, _) = report("el", "centos6", Some("centos5-to-alma10"));
    assert!(summary.contains("only centos7, alma9, alma10"), "{summary}");
    // CentOS 7: its own route only, not the rest of CentOS 5's
    let (summary, _, _, names) = report("el", "centos7", None);
    assert_eq!(names, "centos7-to-alma10");
    assert!(!summary.contains("joins this route"), "{summary}");
    // Scientific Linux 7 joins the ladder at centos7, so the same
    assert_eq!(report("el", "sl7", None).3, "centos7-to-alma10");
    // Debian 10 and Ubuntu 18.04
    let (summary, _, _, names) = report("debian", "buster", None);
    assert_eq!(names, "etch-to-trixie");
    assert!(summary.contains("only bullseye, bookworm, trixie"), "{summary}");
    assert_eq!(report("ubuntu", "bionic", None).3, "trusty-to-resolute");
    // from the machine's own release files: AlmaLinux 9 has only the hop to alma10 left
    let alma9 = system(&tree(&[(
        "etc/redhat-release",
        "AlmaLinux release 9.4 (Seafoam Ocelot)\n",
    )]));
    let (summary, _, _, names) = build(Some(&a), None, &alma9, "k", &f, &[]).unwrap();
    assert_eq!(
        names,
        "centos4-to-alma10, centos5-to-alma10, centos7-to-alma10, centos8-to-alma10"
    );
    assert!(summary.contains("so it walks only alma10"), "{summary}");
    let c6 = system(&tree(&[("etc/redhat-release", "CentOS release 6.10 (Final)\n")]));
    assert_eq!(
        build(Some(&a), None, &c6, "k", &f, &[]).unwrap().3,
        "centos4-to-alma10, centos5-to-alma10"
    );
    // at the end of every route: nothing left
    let (summary, _, _, names) = report("el", "alma10", None);
    assert_eq!(names, "");
    assert!(
        summary.contains("No route starts at or passes through el alma10"),
        "{summary}"
    );
}

#[test]
fn long_reports_attach_instead() {
    let short = issue_url("box: x", "x", "none", "sum", "full", "f.md");
    assert!(short.contains("details=full") && short.contains("template=box-report.yml"));
    let long = issue_url("box: x", "x", "none", "sum", &"y".repeat(8000), "f.md");
    assert!(long.len() <= URL_BUDGET && long.contains("details=sum") && long.contains("f.md"));
    assert_eq!(pct("a b/é"), "a%20b%2F%C3%A9");
}

#[test]
fn the_short_link_carries_the_form_and_none_of_the_report() {
    let u = attach_url(
        "box: CentOS Linux release 7.9.2009 (Core)",
        "CentOS Linux release 7.9.2009 (Core)",
        "centos7-to-alma10",
        "wend-box-0123456789ab.md",
    );
    assert!(
        u.starts_with(
            "https://github.com/FieldmouseWorks/wend/issues/new?template=box-report.yml&labels=box-report&title="
        ),
        "{u}"
    );
    assert!(u.contains("&route=centos7-to-alma10&details="), "{u}");
    assert!(u.contains("wend-box-0123456789ab.md"), "{u}");
    // short enough to copy out of a terminal by hand: a few wrapped lines, not a screenful
    assert!(u.len() < 400, "{} characters", u.len());
}

#[test]
fn scrubs_the_names_a_first_stranger_test_found_surviving() {
    let root = tree(&[
        (
            "etc/passwd",
            "root:x:0:0::/root:/bin/bash\nldapgandalf:x:120001:120001::/data/ldapusers/ldapgandalf:/bin/bash\n\
             zx:x:1001:1001::/srv/people/zx:/bin/bash\nnobody:x:65534:65534::/:/sbin/nologin\n\
             nfsnobody:x:4294967294:4294967294::/:/sbin/nologin\n",
        ),
        ("etc/hostname", "strangelove-db01.corp.zylkodomain.example\n"),
    ]);
    let words = local_words(&root, "strangelove-db01.corp.zylkodomain.example", &[]);
    let s = scrub(
        "/data/ldapusers/ldapgandalf/app.py ; /srv/people/zx/tool.py ; /dev/mapper/vg_strangelovedb01-lv_app ; \
         /srv/corp.zylkodomain.example/sync.py ; /srv/zylkodomain.example/x ; /srv/zylkodomain/y ; \
         /dev/mapper/centos_oldhost--mx7-home ; /dev/mapper/oldbox--vg-root ; \
         (cron, /etc/cron.d/zylkocron-billing) ; nobody ; nfsnobody",
        &words,
    );
    for gone in [
        // a login account above 65534 (LDAP ranges start there), and a two-letter one
        "ldapgandalf",
        "/zx/",
        // the hostname as an installer writes it into a volume group: dashes dropped
        "strangelovedb01",
        // the domain alone, at each level
        "zylkodomain",
        // a volume group named after a hostname the machine no longer has
        "oldhost",
        "mx7",
        "oldbox",
        // a cron.d file is named by whoever wrote it
        "zylkocron",
    ] {
        assert!(!s.contains(gone), "{gone} survived: {s}");
    }
    assert!(s.contains("/dev/mapper/vg_<local>-lv_app"), "{s}");
    assert!(s.contains("/dev/mapper/centos_<local>-home"), "{s}");
    assert!(s.contains("/dev/mapper/<local>--vg-root"), "{s}");
    assert!(s.contains("(cron, /etc/cron.d/<file>)"), "{s}");
    // the accounts every machine has are not names of this one
    assert!(s.ends_with("; nobody ; nfsnobody"), "{s}");
    assert!(!words.iter().any(|w| w == "example" || w == "corp"), "{words:?}");
}

#[test]
fn scrubs_the_domain_whatever_its_shape() {
    for (host, text, gone) in [
        // a name with no public suffix at all
        (
            "web01.acmecorp",
            "/srv/acmecorp/app.py /srv/web01/x",
            vec!["acmecorp", "web01"],
        ),
        // a suffix of two parts: the name before it stands alone too
        (
            "web01.acme.co.uk",
            "/srv/acme/app.py /srv/acme.co.uk/x mail.acme.co.uk",
            vec!["acme"],
        ),
        (
            "db.internal.zylko.example",
            "/srv/zylko/a /srv/internal.zylko.example/b",
            vec!["zylko"],
        ),
    ] {
        let root = tree(&[("etc/hostname", &format!("{host}\n")[..])]);
        let words = local_words(&root, host, &[]);
        let s = scrub(text, &words);
        for g in gone {
            assert!(!s.contains(g), "{host}: {g} survived: {s} (words {words:?})");
        }
        // the words every name is made of are nobody's
        for kept in ["co", "uk", "internal", "example", "local", "com"] {
            assert!(!words.iter().any(|w| w == kept), "{host}: {kept} is a word: {words:?}");
        }
    }
    // a suffix word in a path is left alone
    let root = tree(&[("etc/hostname", "web01.example.com\n")]);
    let words = local_words(&root, "web01.example.com", &[]);
    assert_eq!(
        scrub("/usr/local/com/internal/example", &words),
        "/usr/local/com/internal/example"
    );
}

#[test]
fn scrubs_secrets_however_they_are_named_and_leaves_what_follows_them() {
    let s = scrub(
        r#"{"a": "/srv/x/DB_PASSWORD=EnvPathPl4nted/run.py", "b": "password=EqPathPl4nted", "c": "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U", "d": "mytoken: abc123"}"#,
        &[],
    );
    for gone in ["EnvPathPl4nted", "EqPathPl4nted", "eyJhbGci", "abc123"] {
        assert!(!s.contains(gone), "{gone} survived: {s}");
    }
    // a value ends at its closing quote: the report's facts are still JSON
    assert!(serde_json::from_str::<Value>(&s).is_ok(), "{s}");
}

#[test]
fn a_unit_with_an_at_sign_is_not_an_email_where_a_unit_stands() {
    // where the report puts a unit: in brackets after the program it runs, and as the facts' "where"
    let kept = "/opt/a.py runs on Python 2.7 (serial-getty@ttyS0.service); \
                /opt/b.py has Python 2.7 loaded (openvpn@server.service); \
                /opt/c.py runs on Python 2.6, installed by conary (mint:python) (postgresql@12-main.service) \
                \"where\": \"backup@nightly.timer\"";
    assert_eq!(scrub(kept, &[]), kept);
    // anywhere else, something shaped like an address is one, whatever it ends in
    let s = scrub(
        "/srv/ops@acme.service/run.py runs on Python 2.7 (cron, /etc/crontab); mail ops@acme.example; \
         (ops@acme.service) \"program\": \"/home/x/ops@acme.timer\"",
        &[],
    );
    assert!(!s.contains("ops@"), "{s}");
    assert_eq!(s.matches("<email>").count(), 4, "{s}");
}

#[test]
fn scrubs_a_secret_inside_quotes_and_keeps_the_quotes() {
    for (text, want) in [
        (r#"password="hunter2""#, r#"password="<redacted>""#),
        (r#""password":"hunter2""#, r#""password":"<redacted>""#),
        (
            r#""db_password": "hunter2", "port": 5432"#,
            r#""db_password": "<redacted>", "port": 5432"#,
        ),
        // a value in quotes can have spaces in it: all of it goes
        (r#"secret = 'two words here' next"#, r#"secret = '<redacted>' next"#),
        (r#"api_key: "a b"; x"#, r#"api_key: "<redacted>"; x"#),
        // unquoted, as before
        ("password=hunter2 next", "password=<redacted> next"),
        ("token: abc123,", "token: <redacted>,"),
    ] {
        assert_eq!(scrub(text, &[]), want, "{text}");
    }
    // inside a JSON string the quotes are escaped: the value goes, and what is left is still JSON
    let s = scrub(
        r#"{"cmd": "run --password=\"hunter two\" --name=\"kept\" now", "n": 1}"#,
        &[],
    );
    assert!(!s.contains("hunter"), "{s}");
    let v: Value = serde_json::from_str(&s).unwrap_or_else(|e| panic!("{e}: {s}"));
    assert_eq!(v["cmd"], "run --password=\"<redacted>\" --name=\"kept\" now");
}

#[test]
fn names_rebuilds_that_are_not_centos() {
    let sl7 = tree(&[("etc/redhat-release", "Scientific Linux release 7.9 (Nitrogen)\n")]);
    assert_eq!(system(&sl7).release, Some(("el".into(), "sl7".into())));
    // Oracle Linux's redhat-release says Red Hat, for the programs that look there; oracle-release says what it is
    let ol7 = tree(&[
        (
            "etc/redhat-release",
            "Red Hat Enterprise Linux Server release 7.9 (Maipo)\n",
        ),
        ("etc/oracle-release", "Oracle Linux Server release 7.9\n"),
    ]);
    let s = system(&ol7);
    assert_eq!(
        (s.pretty.as_str(), s.release),
        ("Oracle Linux Server release 7.9", None)
    );
}

#[test]
fn a_report_made_without_the_atlas_says_so() {
    let sys = System {
        pretty: "CentOS Linux release 7.9.2009 (Core)".into(),
        release: Some(("el".into(), "centos7".into())),
    };
    let f = serde_json::json!({"arch": "x86_64", "cpu": {"model": "test", "features": []}, "boot": "bios",
        "ram_mib": 4096, "free_mib": 40000, "boot_dir": {"separate": false, "total_mib": 1, "free_mib": 1, "kernels": []},
        "mounts": [], "runtimes": [], "native": [], "python2_modules": []});
    let (summary, full, _, _) = render(&sys, "k", &f, &[], &[], false);
    assert!(full.contains("wend was started without its atlas"), "{full}");
    assert!(
        !full.contains("No route starts") && !summary.contains("No route starts"),
        "{full}"
    );
    // with the atlas and no route, it still says there is none
    let (_, full, _, _) = render(&sys, "k", &f, &[], &[], true);
    assert!(
        full.contains("No route starts at or passes through el centos7 yet"),
        "{full}"
    );
}
