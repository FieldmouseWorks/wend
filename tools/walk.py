"""Walk a route on a box: replay (no model) and discover (a model fills in the hops that break).

The map walk from dep-lab: on each hop, apply every pinned fix whose `when` holds on the box right now; keep the hop
only if every command succeeds and every check that passed before still passes, otherwise roll the box back.
With a model, a hop that fails asks it for the missing fix. Proposals go through the same schema check as a PR
(check.check_fix) and must hold on this box before they run; kept ones become fix files.
"""
import hashlib
import json
import os
import re
import time

import backends
import check
import sandbox
import shelter
from backends import fill, pred_sh
from sandbox import Box, safe_rm, tail

ADDED = "/var/lib/wend/added-families"  # SPEC "restores and adds"


class Walk:
    def __init__(self, repo, route_doc, box_id, log=lambda m: print(m, flush=True)):
        self.repo, self.route, self.box_id, self.log_ = repo, route_doc, box_id, log
        self.family = route_doc["family"]
        self.adapter = repo.adapters[self.family]
        self.box_doc = repo.boxes[box_id]
        self.checks = sandbox.checks_for(self.box_doc, self.adapter)
        self.isolate = set(self.box_doc.get("isolate", []))
        self.shelter_plan = self.box_doc.get("shelter", {})  # SPEC "Sheltering an app": the owner's confirmed plan
        self.shelter_steps, self.shelters = {}, []
        self.vars = {}
        self.t0 = time.time()
        sandbox.ensure_mirror(self.adapter)
        self.stamp = time.strftime("%Y%m%d-%H%M%S")
        base = sandbox.build_box(box_id, self.box_doc, self.adapter, repo.raw[f"boxes/{box_id}/box.toml"],
                                 repo.raw[f"adapters/{self.family}.toml"], log=self.log)
        self.dir = f"{sandbox.WORK}/runs/{self.stamp}-{box_id.replace('/', '_')}-{os.getpid()}"
        self.box = base.copy_to(f"{self.dir}/root")
        self.snap = f"{self.dir}/snap"
        self.health = self.check_health()
        rc, pk = self.box.run(self.adapter["hop"]["installed_list"])
        self.fingerprint = "sha256:" + hashlib.sha256(check.canonical(
            {"family": self.family, "release": self.box_doc["release"], "arch": self.box_doc.get("arch", "amd64"),
             "installed": sorted(pk.split())})).hexdigest()
        self.services = sorted(self.box.run("ls /etc/init.d 2>/dev/null")[1].split())
        self.steps, self.hops = [], []

    def check_health(self):
        if self.shelter_plan and shelter.has_shelter(self.box, self.shelter_plan):
            with shelter.standin(self.box):  # chroot tier: the podman stand-in, only while the checks run
                return self.box.health(self.checks, self.adapter["release_file"], self.isolate)
        return self.box.health(self.checks, self.adapter["release_file"], self.isolate)

    def log(self, msg):
        self.log_(f"[{self.box_id} {time.time() - self.t0:5.0f}s] {msg}")

    def holds(self, fix):
        return self.box.run(f"{pred_sh(fix['when'], self.adapter)} >/dev/null 2>&1")[0] == 0

    def hop_cmds(self, hop, fixes):
        """SPEC.md "How a hop runs": set the release, refresh, the kernel handover's first half, pre steps, the upgrade,
        its second half, post steps (then checks)."""
        first, second = Walk.hop_parts(self, hop, fixes)  # works on the bare walks parity.py and vm.py make
        return first + second

    def hop_parts(self, hop, fixes):
        """hop_cmds in two parts: through the upgrade transaction, and the rest. A release the adapter marks
        reboot = "before-post" boots its new kernel between them on a real machine (tools/vm.py); a chroot runs both
        straight through."""
        a, h = self.adapter, self.adapter["hop"]
        self.vars = v = backends.of(a).hop_vars(a, hop)
        comp = lambda ph: [check.compile_step(s, a, hop) for f in fixes for s in f.get(ph, [])]
        # SPEC "Sheltering an app": on the hop that drops Python 2, stop-native first in pre, start last in post
        sh_pre, sh_post = getattr(self, "shelter_steps", {}).get(hop, ([], []))
        opts = {s["option"] for f in fixes for s in f.get("pre", []) + f.get("post", []) if s["op"] == "pm-option"}
        cleanup = [f"rm -f {a['pm_options'][o]['path']}" for o in sorted(opts)]  # pm-options are hop-scoped
        rel = a["releases"].get(hop)
        upgrade = rel["upgrade"] if isinstance(rel, dict) and "upgrade" in rel else h["upgrade"]
        verify = [fill(h["verify"], v)] if "verify" in h else []  # the release file says we arrived
        # SPEC "restores and adds": the families the fired fixes may introduce, for the services check
        if not all(isinstance(f.get("adds", []), list) for f in fixes):
            raise ValueError("adds must be a list of name families")
        adds = sorted({x for f in fixes for x in f.get("adds", [])})
        if not all(isinstance(x, str) and re.fullmatch(check.FAMILY, x) for x in adds):  # never shell, on any path
            raise ValueError(f"adds must be name families ({check.FAMILY}), not {adds!r}")
        for f in fixes:  # the checker's rule again: a direct `wend replay` doesn't run check_fix first
            if set(f.get("adds", [])) - check.installed_families(f):
                raise ValueError(f"{f.get('meta', {}).get('name')}: adds names a family its own install steps "
                                 "don't install")
        declare = [f"mkdir -p /var/lib/wend && printf '{''.join(x + chr(92) + 'n' for x in adds)}' >> {ADDED}"] if adds else []
        kernel = lambda k: [fill(c, v) for c in h.get(k, [])]  # noqa: E731 (SPEC "Kernels and the bootloader")
        # before_transaction and after_transaction: the adapter's own around everything that runs a transaction
        return ([fill(h["set_release"], v), fill(h["refresh"], v)] + kernel("kernel_before") +
                kernel("before_transaction") + sh_pre + comp("pre") + [fill(c, v) for c in upgrade] +
                kernel("kernel_after") + kernel("after_transaction"),
                comp("post") + sh_post + declare + cleanup + verify)

    def execute(self, commands, why):
        """Run commands in order; keep the step only if all succeed and no passing check is lost."""
        t0 = time.time()
        self.box.copy_to(self.snap)
        outs, rc = [], 0
        for c in commands:
            rc, out = self.box.run(c)
            outs.append({"cmd": c, "rc": rc, "out": tail(out, 4000)})
            if rc != 0:
                break
        for c in self.adapter["hop"].get("after", []):
            self.box.run(fill(c, self.vars))
        before, h = self.health, self.check_health()
        lost = sorted(set(before["passed"]) - set(h["passed"]))
        ok = rc == 0 and not lost
        stage = "ok" if ok else (f"command failed: {outs[-1]['cmd']}" if rc else f"checks lost: {', '.join(lost)}")
        if ok:
            self.health = h
        else:
            Box(self.snap).copy_to(self.box.root)
        self.steps.append({"why": why, "ok": ok, "stage": stage, "outputs": outs, "health_after": h,
                           "seconds": round(time.time() - t0, 1)})
        self.log(f"{'KEPT' if ok else 'ROLLED BACK'} {why} ({stage}) -> {h['release']} "
                 f"{len(h['passed'])}/{len(self.checks)}")
        return ok

    def prune(self):
        """No-model cleanup: purge packages the target no longer ships, bisecting whenever a check objects."""
        def obsolete():
            rc, out = self.box.run(self.adapter["hop"]["obsolete"])
            return out.split() if rc == 0 else []

        def go(pkgs):
            installed = set(self.box.run(self.adapter["hop"]["installed_list"])[1].split())
            pkgs = [p for p in pkgs if p in installed]
            if pkgs and not self.execute([check.compile_step({"op": "remove", "packages": pkgs, "purge": True},
                                                             self.adapter, self.route["to"]),
                                          self.adapter["hop"]["autoremove"]],
                                         f"prune {len(pkgs)} obsolete packages") and len(pkgs) > 1:
                go(pkgs[:len(pkgs) // 2])
                go(pkgs[len(pkgs) // 2:])
        before = obsolete()
        go(before)
        return {"obsolete_before": len(before), "obsolete_after": len(obsolete())}

    def walk(self, discoverer=None):
        pinned = [self.repo.fixes_by_id[r["id"]] for r in self.route["fixes"]]
        drop = shelter.drop_hop(self.adapter, self.route) if self.shelter_plan else None
        for hop in self.route["hops"]:
            if hop == drop and not self.shelter_before(hop):
                break
            matched = [f for f in pinned if f["hop"] == hop and self.holds(f)]
            names = [f["meta"]["name"] for f in matched]
            ok = self.execute(self.hop_cmds(hop, matched), f"hop to {hop} with {names}")
            new = []
            if not ok and discoverer:
                new = discoverer.fill(self, hop, matched)
                ok = new is not None
                new = new or []
            self.hops.append({"to": hop, "fired": [check.content_id(f) for f in matched + new],
                              "fired_names": names + [f["meta"]["name"] for f in new], "ok": ok,
                              "seconds": round(sum(s["seconds"] for s in self.steps[-1:]), 1)})
            if ok:  # SPEC.md "Pinned sources"
                self.hops[-1]["sources"] = backends.of(self.adapter).sources(self.box.root, self.adapter, hop)
            if ok and hop == drop:  # the id range shelter.start reserved, for the run (not the build's lock)
                for rec in self.shelters:
                    rec["subuid"] = self.box.run(f"awk -F: '$1 == \"wend-{rec['app']}\" {{print $2 \":\" $3}}' "
                                                 "/etc/subuid")[1].strip()
            if not ok:
                break
        extra = self.prune() if self.route.get("finish", {}).get("prune_obsolete") and all(
            h["ok"] for h in self.hops) and len(self.hops) == len(self.route["hops"]) else {}
        final = self.check_health()
        failed = sorted(set(self.checks) - set(final["passed"]))
        complete = len(self.hops) == len(self.route["hops"]) and all(h["ok"] for h in self.hops)
        return {"schema": "wend.run/0", "route": check.content_id(self.route), "box": self.box_id,
                "from": self.route["from"], "to": self.route["to"],
                "result": "pass" if complete and not failed else "fail", "started": self.stamp,
                "wall_seconds": round(time.time() - self.t0), "start_fingerprint": self.fingerprint,
                "runner": runner_info(), "hops": self.hops, "start_services": self.services,
                "failed_stage": next((s["stage"] for s in reversed(self.steps) if not s["ok"]), "") if not complete else "",
                "final": {"release": final["release"], "passed": final["passed"], "failed": failed,
                          "failed_logs": {c: tail(final["checks"][c]["log"], 1500) for c in failed}},
                **({"shelters": self.shelters} if self.shelter_plan else {}),
                **extra, **(discoverer.summary() if discoverer else {"model_calls": 0}),
                "log_sha256": "sha256:" + hashlib.sha256(json.dumps(self.steps, sort_keys=True).encode()).hexdigest()}

    def shelter_before(self, hop):
        """SPEC "Sheltering an app": before the hop that leaves keeps_runtimes_on, build and prove each app's shelter
        on the box as it is (on that release), and queue stop-native and start on the hop. If either fails, the walk
        stops here: the box stays on keeps_runtimes_on with its app running natively (the "stop" option)."""
        keeps = shelter.keeps_runtimes_on(self.adapter, self.route)
        roles = {k: v for k, v in self.checks.items() if k in self.box_doc["roles"]}
        for app, plan in sorted(self.shelter_plan.items()):
            t0 = time.time()
            try:
                rec = shelter.build(self.box, self.adapter, keeps, app, plan, f"{self.dir}/shelter-scratch", self.log)
                h = shelter.prove(self.box, app, plan, keeps, roles, self.isolate, self.adapter["release_file"],
                                  self.health["passed"], f"{self.dir}/shelter-scratch", self.log)
            except shelter.Refused as e:
                self.steps.append({"why": f"shelter {app} on {keeps}", "ok": False,
                                   "stage": f"shelter {app}: {str(e).splitlines()[0]}",
                                   "outputs": [{"cmd": "shelter.build; shelter.prove", "rc": 1, "out": tail(str(e), 4000)}],
                                   "health_after": self.health, "seconds": round(time.time() - t0, 1)})
                self.log(f"REFUSED shelter {app} on {keeps}: {tail(str(e), 800)}")
                return False
            self.shelters.append({k: rec[k] for k in ("app", "release", "lock_sha256", "image", "tier",
                                                      "confinement_proved")})
            self.steps.append({"why": f"shelter {app} on {keeps}", "ok": True, "stage": "ok",
                               "outputs": [{"cmd": "shelter.build; shelter.prove", "rc": 0,
                                            "out": f"{rec['packages']} packages; carried: {rec['carried']}"}],
                               "health_after": h, "seconds": round(time.time() - t0, 1)})
            self.log(f"KEPT shelter {app}: built on {keeps} and proved ({rec['packages']} packages)")
            pre, post = self.shelter_steps.setdefault(hop, ([], []))
            pre += shelter.stop_native(app, plan)
            post += shelter.start(app, plan, self.adapter, hop, keeps)
        return True

    def close(self, keep=False):
        if not keep:
            safe_rm(self.dir)


def runner_info():
    if os.environ.get("GITHUB_ACTIONS"):
        return {"kind": "github-actions", "image": os.environ.get("ImageOS", ""),
                "run_url": f"{os.environ.get('GITHUB_SERVER_URL')}/{os.environ.get('GITHUB_REPOSITORY')}/actions/runs/"
                           f"{os.environ.get('GITHUB_RUN_ID')}", "harness": os.environ.get("GITHUB_SHA", "")}
    return {"kind": "local", "host": os.uname().nodename, "kernel": os.uname().release}
