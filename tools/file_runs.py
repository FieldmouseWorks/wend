"""File CI's fresh run.json artifacts under runs/<family>/<route name>/<box name>/<stamp>.json.

  python3 tools/file_runs.py DIR      DIR holds downloaded artifacts (any depth)
"""
import glob
import json
import os
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
    json.dump(run, open(dst, "w"), indent=1)
    print(os.path.relpath(dst, ROOT))
