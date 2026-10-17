//! SPEC "Checks the machine is held to", the generated checks: what the machine runs at the moment of the copy,
//! read from it over the rehearsal's ssh, becomes checks the walk holds the copy to (the offline form, starting the
//! daemon inside the root as a CI box's roles do) and checks `upgrade` will hold the booted machine to (the live
//! form, asking the running service). The adapter's `[generated]` names the services and how to read them.

use crate::atlas::{get, Doc};
use serde_json::{json, Map, Value};

/// What the machine runs, as the probe read it.
#[derive(Default, Debug, PartialEq)]
pub struct Seen {
    /// services enabled at boot and running, each with every name it goes by on the route
    pub services: Vec<(String, Vec<String>)>,
    /// TCP ports a selected service listens on
    pub ports: Vec<(String, u16)>,
    /// name-based virtual hosts on port 80 and the status `/` answered with on localhost
    pub vhosts: Vec<(String, String)>,
    /// databases in the MySQL/MariaDB data directory and the tables each holds
    pub dbs: Vec<(String, Vec<String>)>,
    /// cron entries the owner wrote: file, then the line as it is there
    pub cron: Vec<(String, String)>,
}

/// Shell single quotes.
fn sq(s: &str) -> String {
    format!("'{}'", s.replace('\'', "'\\''"))
}

fn strings(v: Option<&Value>) -> Vec<String> {
    v.and_then(Value::as_array)
        .into_iter()
        .flatten()
        .filter_map(|x| x.as_str().map(String::from))
        .collect()
}

/// The adapter's services: name, then every name it goes by (the first is its own).
fn services(g: &Doc) -> Vec<(String, Vec<String>)> {
    get(g, "services")
        .and_then(Value::as_object)
        .into_iter()
        .flatten()
        .map(|(k, v)| (k.clone(), strings(Some(v))))
        .collect()
}

/// The first probe, read-only on the machine: tab-separated `svc NAME`, `net LINE` (a listening socket), `vhost
/// LINE` (`httpd -S`), `db NAME`, `table DB NAME` and `cron FILE LINE`. Plain sh, as CentOS 5's runs it.
pub fn probe(g: &Doc) -> String {
    let names: Vec<String> = services(g).into_iter().map(|(n, _)| n).collect();
    let owned = get(g, "owned").and_then(Value::as_str).unwrap_or("false");
    let modified = get(g, "modified").and_then(Value::as_str).unwrap_or("false");
    let web = strings(get(g, "web"));
    let db = strings(get(g, "db"));
    let entries = "grep -v -E '^[[:space:]]*(#|$)' \"$f\" | grep -v -E '^[[:space:]]*[A-Za-z_][A-Za-z0-9_]*=' \
                   | while IFS= read -r l; do printf 'cron\\t%s\\t%s\\n' \"$f\" \"$l\"; done";
    format!(
        "if [ -d /run/systemd/system ]; then en() {{ systemctl is-enabled \"$1\" 2>/dev/null | grep -qx enabled; }}; \
         up() {{ systemctl is-active \"$1\" >/dev/null 2>&1; }}; \
         else en() {{ chkconfig --list \"$1\" 2>/dev/null | grep -q '3:on'; }}; \
         up() {{ service \"$1\" status >/dev/null 2>&1; }}; fi; \
         ON=''; for s in {names}; do en $s && up $s && {{ printf 'svc\\t%s\\n' $s; ON=\"$ON $s \"; }}; done; \
         (netstat -ltnp 2>/dev/null || ss -ltnp 2>/dev/null) | while IFS= read -r l; do printf 'net\\t%s\\n' \"$l\"; done; \
         for s in {web}; do case \"$ON\" in *\" $s \"*) $s -S 2>&1 | while IFS= read -r l; do printf 'vhost\\t%s\\n' \"$l\"; done;; esac; done; \
         for s in {db}; do case \"$ON\" in *\" $s \"*) \
         D=$(my_print_defaults mysqld 2>/dev/null | sed -n 's/^--datadir=//p' | tail -n1); [ -n \"$D\" ] || D=/var/lib/mysql; \
         for d in \"$D\"/*/; do [ -d \"$d\" ] || continue; b=$(basename \"$d\"); \
         case $b in mysql|performance_schema|sys|lost+found|*@*|*'#'*) continue;; esac; printf 'db\\t%s\\n' \"$b\"; \
         for t in \"$d\"*.frm; do [ -e \"$t\" ] && printf 'table\\t%s\\t%s\\n' \"$b\" \"$(basename \"$t\" .frm)\"; done; done; break;; esac; done; \
         for f in /var/spool/cron/*; do [ -f \"$f\" ] && {entries}; done; \
         for f in /etc/cron.d/*; do [ -f \"$f\" ] && ! {owned} \"$f\" >/dev/null 2>&1 && {entries}; done; \
         f=/etc/crontab; [ -f $f ] && {modified} $f 2>/dev/null | grep -q \" $f$\" && \
         grep -v -E '^[[:space:]]*(#|$)' $f | grep -v -E '^[[:space:]]*[A-Za-z_][A-Za-z0-9_]*=' | grep -v run-parts \
         | while IFS= read -r l; do printf 'cron\\t%s\\t%s\\n' $f \"$l\"; done; true",
        names = names.join(" "),
        web = web.join(" "),
        db = db.join(" "),
    )
}

/// The second probe: the status each vhost's `/` answers with on localhost.
pub fn status_probe(vhosts: &[String]) -> String {
    vhosts
        .iter()
        .map(|h| {
            format!(
                "printf '%s\\t%s\\n' {} \"$(curl -s -o /dev/null -m 20 -w '%{{http_code}}' -H {} http://127.0.0.1/)\"",
                sq(h),
                sq(&format!("Host: {h}"))
            )
        })
        .collect::<Vec<_>>()
        .join("; ")
}

/// The listening socket lines' (program, port): netstat's `PID/prog` or ss's `users:(("prog",...`.
fn listener(l: &str) -> Option<(String, u16)> {
    let f: Vec<&str> = l.split_whitespace().collect();
    if !l.contains("LISTEN") || f.len() < 4 {
        return None;
    }
    // netstat: tcp 0 0 LOCAL ...; ss: LISTEN 0 128 LOCAL ...
    let local = f[3];
    let port = local.rsplit([':', '.']).next()?.parse().ok()?;
    let prog = if let Some(u) = l.split("((\"").nth(1) {
        u.split('"').next()?.to_string()
    } else {
        f.last()?.split_once('/')?.1.to_string()
    };
    Some((prog, port))
}

/// Read the first probe. `vhost` lines name the port-80 name-based hosts (`httpd -S`: `port 80 namevhost NAME (`).
pub fn read(g: &Doc, out: &str) -> Seen {
    let known = services(g);
    let mut s = Seen::default();
    let mut nets = vec![];
    for l in out.lines() {
        let mut p = l.splitn(3, '\t');
        match (p.next(), p.next(), p.next()) {
            (Some("svc"), Some(n), None) => {
                let names = known
                    .iter()
                    .find(|(k, _)| k == n)
                    .map(|(_, v)| v.clone())
                    .unwrap_or_default();
                // one service by any of its names (mysqld and mariadb are one on CentOS 7)
                if !s
                    .services
                    .iter()
                    .any(|(_, v)| v.iter().any(|x| x == n || names.contains(x)))
                {
                    s.services.push((
                        n.to_string(),
                        if names.is_empty() { vec![n.to_string()] } else { names },
                    ));
                }
            }
            (Some("net"), Some(line), None) => nets.extend(listener(line)),
            (Some("vhost"), Some(line), None) => {
                let w: Vec<&str> = line.split_whitespace().collect();
                if let Some(i) = w.iter().position(|x| *x == "namevhost") {
                    if i >= 2 && w[i - 2] == "port" && w[i - 1] == "80" {
                        if let Some(h) = w.get(i + 1) {
                            if !s.vhosts.iter().any(|(x, _)| x == h) {
                                s.vhosts.push((h.to_string(), String::new()));
                            }
                        }
                    }
                }
            }
            (Some("db"), Some(n), None) => s.dbs.push((n.to_string(), vec![])),
            (Some("table"), Some(d), Some(t)) => {
                if let Some(e) = s.dbs.iter_mut().find(|(x, _)| x == d) {
                    e.1.push(t.to_string());
                }
            }
            (Some("cron"), Some(f), Some(line)) => s.cron.push((f.to_string(), line.to_string())),
            _ => {}
        }
    }
    // a selected service's own ports: its program is named for it (mariadbd for mysqld, postfix's master)
    let progs = |n: &str| -> Vec<String> {
        let mut v = vec![n.to_string()];
        match n {
            "mysqld" | "mariadb" => v.extend(["mysqld".into(), "mariadbd".into()]),
            "postfix" => v.push("master".into()),
            _ => {}
        }
        v
    };
    for (prog, port) in nets {
        if let Some((n, _)) = s.services.iter().find(|(n, _)| progs(n).contains(&prog)) {
            if !s.ports.contains(&(n.clone(), port)) {
                s.ports.push((n.clone(), port));
            }
        }
    }
    s.ports.sort();
    s
}

/// Fill in the vhosts' statuses from the second probe; a host that didn't answer on localhost isn't held.
pub fn read_status(s: &mut Seen, out: &str) {
    for l in out.lines() {
        if let Some((h, code)) = l.split_once('\t') {
            if let Some(e) = s.vhosts.iter_mut().find(|(x, _)| x == h) {
                e.1 = code.trim().to_string();
            }
        }
    }
    s.vhosts.retain(|(_, c)| c.len() == 3 && c != "000");
}

const ENABLED: &str = "en() { if [ -x /usr/bin/systemctl ] || [ -x /bin/systemctl ]; then \
                       systemctl is-enabled \"$1\" 2>/dev/null | grep -qx enabled; \
                       else chkconfig --list \"$1\" 2>/dev/null | grep -q '3:on'; fi; }";

/// mysqld from the root, started as the box files start it, with `extra` options; `$P` is its pid file.
fn start_db(extra: &str) -> String {
    format!(
        "mkdir -p /var/run/mysqld /run/mariadb /var/run/mariadb 2>/dev/null; chown mysql /var/run/mysqld /run/mariadb \
         /var/run/mariadb 2>/dev/null; M=$(ls /usr/libexec/mariadbd /usr/libexec/mysqld /usr/sbin/mariadbd \
         /usr/sbin/mysqld 2>/dev/null | head -n1); P=/tmp/wend-gen-db.pid; rm -f $P; \
         ($M --user=mysql --pid-file=$P {extra} >/tmp/wend-gen-db.log 2>&1 &); i=0; \
         while [ $i -lt 90 ] && [ ! -S \"$S\" ]; do sleep 1; i=$((i+1)); done; \
         [ -S \"$S\" ] || {{ echo 'the database server did not start:'; tail -n 15 /tmp/wend-gen-db.log; exit 1; }}"
    )
}

const STOP_DB: &str = "[ -f $P ] && kill $(cat $P) 2>/dev/null; i=0; while [ $i -lt 120 ] && [ -f $P ] && \
                       kill -0 $(cat $P) 2>/dev/null; do sleep 1; i=$((i+1)); done";

const SOCKET: &str = "S=$(my_print_defaults mysqld 2>/dev/null | sed -n 's/^--socket=//p' | tail -n1); \
                      [ -n \"$S\" ] || S=/var/lib/mysql/mysql.sock";

/// The `[generated]` table appended to the checks file: offline roles the walk runs, which of them run isolated,
/// and the live forms `upgrade` will run on the booted machine.
pub fn render(g: &Doc, s: &Seen) -> Value {
    let mut roles = Map::new();
    let mut live = Map::new();
    let mut isolate = vec![];
    if !s.services.is_empty() {
        let body: Vec<String> = s
            .services
            .iter()
            .map(|(n, names)| {
                format!(
                    "{{ {}; }} || {{ echo {}; f=1; }}",
                    names.iter().map(|x| format!("en {x}")).collect::<Vec<_>>().join(" || "),
                    sq(&format!("{n} does not start at boot (as {})", names.join(" or ")))
                )
            })
            .collect();
        roles.insert(
            "gen-services".into(),
            json!(format!("{ENABLED}; f=0; {}; exit $f", body.join("; "))),
        );
        let up: Vec<String> = s
            .services
            .iter()
            .map(|(n, names)| {
                format!(
                    "{{ {}; }} || {{ echo {}; f=1; }}",
                    names
                        .iter()
                        .map(|x| format!("systemctl is-active {x} >/dev/null 2>&1"))
                        .collect::<Vec<_>>()
                        .join(" || "),
                    sq(&format!("{n} is not running"))
                )
            })
            .collect();
        live.insert("gen-services".into(), json!(format!("f=0; {}; exit $f", up.join("; "))));
    }
    if !s.ports.is_empty() {
        let body: Vec<String> = s
            .ports
            .iter()
            .map(|(n, p)| {
                format!(
                    "echo \"$L\" | grep -Eq '[:.]{p}$' || {{ echo {}; f=1; }}",
                    sq(&format!("nothing listens on {p} ({n} did)"))
                )
            })
            .collect();
        live.insert(
            "gen-ports".into(),
            json!(format!(
                "L=$( (ss -ltn 2>/dev/null || netstat -ltn) | awk '{{print $4}}'); f=0; {}; exit $f",
                body.join("; ")
            )),
        );
    }
    let on = |list: &str| {
        strings(get(g, list))
            .iter()
            .any(|w| s.services.iter().any(|(n, _)| n == w))
    };
    let db_on = on("db");
    if on("web") {
        let start_db = if db_on {
            format!("{SOCKET}; {}; ", start_db(""))
        } else {
            String::new()
        };
        let mut body = format!(
            "{start_db}mkdir -p /run/php-fpm /var/run/httpd 2>/dev/null; test -x /usr/sbin/php-fpm && /usr/sbin/php-fpm -D; \
             /usr/sbin/httpd -t 2>&1 || {{ echo 'httpd -t fails'; exit 1; }}; /usr/sbin/httpd -k start; i=0; \
             while [ $i -lt 30 ] && ! curl -s -o /dev/null -m 5 http://127.0.0.1/; do sleep 1; i=$((i+1)); done; f=0"
        );
        let mut lv = String::from("f=0");
        for (h, code) in &s.vhosts {
            let get = format!(
                "c=$(curl -s -o /dev/null -m 20 -w '%{{http_code}}' -H {} http://127.0.0.1/); [ \"$c\" = {code} ] || {{ echo {} \"$c\"; f=1; }}",
                sq(&format!("Host: {h}")),
                sq(&format!("{h}/ answered {code} before, now"))
            );
            body.push_str(&format!("; {get}"));
            lv.push_str(&format!("; {get}"));
        }
        body.push_str("; /usr/sbin/httpd -k stop >/dev/null 2>&1");
        if db_on {
            body.push_str(&format!("; {STOP_DB}"));
        }
        roles.insert("gen-web".into(), json!(format!("{body}; exit $f")));
        isolate.push("gen-web");
        live.insert(
            "gen-web".into(),
            json!(format!("httpd -t 2>&1 || exit 1; {lv}; exit $f")),
        );
    }
    if s.services.iter().any(|(n, _)| n == "nginx") {
        roles.insert("gen-nginx".into(), json!("nginx -t"));
        live.insert("gen-nginx".into(), json!("nginx -t"));
    }
    if db_on && !s.dbs.is_empty() {
        let mut body = format!(
            "S=/tmp/wend-gen-db.sock; {}; C=$(command -v mariadb || command -v mysql); f=0",
            start_db("--skip-grant-tables --skip-networking --socket=$S")
        );
        let mut lv = String::from("C=$(command -v mariadb || command -v mysql); f=0");
        for (d, tables) in &s.dbs {
            let list = format!("$($C -S $S -N -e {} 2>&1)", sq(&format!("show tables from `{d}`")));
            body.push_str(&format!("; T={list}"));
            lv.push_str(&format!(
                "; T=$($C -N -e {} 2>&1)",
                sq(&format!("show tables from `{d}`"))
            ));
            let want: Vec<String> = if tables.is_empty() {
                vec![format!(
                    "{{ $C -S $S -N -e 'show databases' | grep -qxF {} || {{ echo {}; f=1; }}; }}",
                    sq(d),
                    sq(&format!("database {d} is gone"))
                )]
            } else {
                tables
                    .iter()
                    .map(|t| {
                        format!(
                            "{{ echo \"$T\" | grep -qxF {} || {{ echo {}; f=1; }}; }}",
                            sq(t),
                            sq(&format!("table {d}.{t} is gone"))
                        )
                    })
                    .collect()
            };
            for w in &want {
                body.push_str(&format!("; {w}"));
                if !tables.is_empty() {
                    lv.push_str(&format!("; {w}"));
                }
            }
        }
        body.push_str(&format!(
            "; {STOP_DB}; [ $f = 0 ] || tail -n 5 /tmp/wend-gen-db.log; exit $f"
        ));
        roles.insert("gen-db".into(), json!(body));
        isolate.push("gen-db");
        live.insert("gen-db".into(), json!(format!("{lv}; exit $f")));
    }
    if !s.cron.is_empty() {
        let body: Vec<String> = s
            .cron
            .iter()
            .map(|(file, line)| {
                format!(
                    "grep -qxF -- {} {} || {{ echo {}; f=1; }}",
                    sq(line),
                    sq(file),
                    sq(&format!("{file} lost: {line}"))
                )
            })
            .collect();
        let c = format!("f=0; {}; exit $f", body.join("; "));
        roles.insert("gen-cron".into(), json!(c));
        live.insert("gen-cron".into(), json!(c));
    }
    json!({"isolate": isolate, "roles": roles, "live": live})
}

#[cfg(test)]
mod tests {
    use super::*;

    fn adapter() -> Doc {
        json!({"services": {"httpd": ["httpd"], "mysqld": ["mysqld", "mariadb"], "mariadb": ["mariadb", "mysqld"],
                            "postfix": ["postfix"], "sshd": ["sshd"]},
               "web": ["httpd"], "db": ["mysqld", "mariadb"], "owned": "rpm -qf", "modified": "rpm -Vf"})
    }

    #[test]
    fn reads_what_centos5_and_7_probes_print() {
        let out = "svc\thttpd\nsvc\tmysqld\nsvc\tmariadb\nsvc\tpostfix\n\
                   net\ttcp        0      0 0.0.0.0:3306   0.0.0.0:*   LISTEN      2011/mysqld\n\
                   net\ttcp        0      0 :::80          :::*        LISTEN      2101/httpd\n\
                   net\ttcp        0      0 127.0.0.1:25   0.0.0.0:*   LISTEN      1990/master\n\
                   net\ttcp        0      0 0.0.0.0:111    0.0.0.0:*   LISTEN      1500/portmap\n\
                   net\tLISTEN 0 128 *:2222 *:* users:((\"sshd\",pid=1,fd=3))\n\
                   vhost\tVirtualHost configuration:\n\
                   vhost\t*:80                   is a NameVirtualHost\n\
                   vhost\t         port 80 namevhost mysite.local (/etc/httpd/conf.d/mysite.conf:2)\n\
                   vhost\t         port 443 namevhost secure.local (/etc/httpd/conf.d/ssl.conf:74)\n\
                   db\tapp\ntable\tapp\tt\ndb\tempty\n\
                   cron\t/var/spool/cron/alice\t0 3 * * * /home/alice/nightly-backup\n";
        let s = read(&adapter(), out);
        assert_eq!(
            s.services,
            vec![
                ("httpd".into(), vec!["httpd".into()]),
                ("mysqld".into(), vec!["mysqld".into(), "mariadb".into()]),
                ("postfix".into(), vec!["postfix".into()])
            ]
        );
        assert_eq!(
            s.ports,
            vec![("httpd".into(), 80), ("mysqld".into(), 3306), ("postfix".into(), 25)]
        );
        assert_eq!(s.vhosts, vec![("mysite.local".into(), String::new())]);
        assert_eq!(s.dbs, vec![("app".into(), vec!["t".into()]), ("empty".into(), vec![])]);
        assert_eq!(s.cron.len(), 1);
        let mut s = s;
        read_status(&mut s, "mysite.local\t200\n");
        assert_eq!(s.vhosts, vec![("mysite.local".into(), "200".into())]);
        read_status(&mut s, "mysite.local\t000\n");
        assert!(s.vhosts.is_empty());
    }

    #[test]
    fn renders_offline_and_live_forms() {
        let mut s = read(
            &adapter(),
            "svc\thttpd\nsvc\tmysqld\nvhost\t port 80 namevhost a.local (x)\ndb\tapp\ntable\tapp\tt\n\
             cron\t/var/spool/cron/alice\t0 3 * * * echo 'hi'\n",
        );
        read_status(&mut s, "a.local\t200\n");
        let g = render(&adapter(), &s);
        let roles = g["roles"].as_object().unwrap();
        assert_eq!(
            roles.keys().collect::<Vec<_>>(),
            vec!["gen-services", "gen-web", "gen-db", "gen-cron"]
        );
        assert_eq!(g["isolate"], json!(["gen-web", "gen-db"]));
        assert!(roles["gen-services"]
            .as_str()
            .unwrap()
            .contains("en mysqld || en mariadb"));
        assert!(roles["gen-web"].as_str().unwrap().contains("[ \"$c\" = 200 ]"));
        assert!(roles["gen-db"].as_str().unwrap().contains("--skip-grant-tables"));
        assert!(roles["gen-cron"]
            .as_str()
            .unwrap()
            .contains("'0 3 * * * echo '\\''hi'\\'''"));
        assert!(g["live"]["gen-services"]
            .as_str()
            .unwrap()
            .contains("systemctl is-active mysqld"));
        // every generated check parses in sh
        for (_, c) in roles.iter().chain(g["live"].as_object().unwrap()) {
            let st = std::process::Command::new("sh")
                .args(["-n", "-c", c.as_str().unwrap()])
                .status()
                .unwrap();
            assert!(st.success(), "{c}");
        }
        let p = probe(&adapter());
        assert!(std::process::Command::new("sh")
            .args(["-n", "-c", &p])
            .status()
            .unwrap()
            .success());
        assert!(std::process::Command::new("sh")
            .args(["-n", "-c", &status_probe(&["a.local".into()])])
            .status()
            .unwrap()
            .success());
    }
}
