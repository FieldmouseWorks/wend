"""File CI's fresh run.json artifacts under runs/<family>/<route name>/<box name>/<stamp>.json.

  gh run download RUN_ID -R FieldmouseWorks/wend -D DIR    the gate run of the PR's head
  python3 tools/file_runs.py DIR                           DIR holds downloaded artifacts (any depth)

Each file is copied byte for byte: the gate attests the artifact's exact bytes, and the schema check accepts a new
or changed run only if `gh attestation verify` finds that attestation (CONTRIBUTING.md, "How runs land").
"""
import glob
import json
import os
import shutil
import sys
import tomllib

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import check  # noqa: E402

names = {check.content_id(tomllib.load(open(p, "rb"))): os.path.basename(p)[:-5]
         for p in glob.glob(f"{ROOT}/routes/*/*.toml")}
for p in glob.glob(f"{sys.argv[1]}/**/run.json", recursive=True):
    run = json.load(open(p))
    fam, box = run["box"].split("/")
    dst = f"{ROOT}/runs/{fam}/{names.get(run['route'], run['route'][7:19])}/{box}/{run['started']}.json"
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.copyfile(p, dst)
    print(os.path.relpath(dst, ROOT))
