#!/usr/bin/env python3
"""Read-only Git-history/current-tree release privacy preflight.

Never prints, stores or exports credential VALUES. Findings contain only a
relative path, an alert category and a short source reference. This is a
heuristic, not full GitHub/license/security certification or a git rewrite.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess

ROOT=Path(__file__).resolve().parents[1]
MAX_FILE_BYTES=8_000_000
BATCH_MAX_BYTES=12_000_000
REPORT_VERSION=1
PATTERNS={
 "github_token":re.compile(rb"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{60,})\b"),
 "provider_secret":re.compile(rb"\b(?:sb_secret_[A-Za-z0-9_-]{18,}|sk-(?:proj-|live-)?[A-Za-z0-9_-]{28,}|xox[baprs]-[A-Za-z0-9-]{18,})\b"),
 "aws_access_key":re.compile(rb"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
 "private_key_header":re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"),
 "personal_absolute_path":re.compile(rb"(?<![A-Za-z0-9:/])(?:/Users/|/home/)[A-Za-z0-9._-]+/"),
}
SECRET_CLASSES={"github_token","provider_secret","aws_access_key","private_key_header"}
UNPUBLISHABLE_DIRECTORIES=(".local/",".codex/",".ai-bridge/",".agents/","outputs/")
UNPUBLISHABLE_NAMES=(".env",".env.local",".env.production",".env.development")
PRIVATE_FILE_SUFFIXES=(".pem",".p12",".pfx",".key",".sqlite3")

def run_git(*args:str,input_bytes:bytes|None=None)->bytes:
 result=subprocess.run(["git",*args],cwd=ROOT,input=input_bytes,stdout=subprocess.PIPE,stderr=subprocess.PIPE,check=True)
 return result.stdout

def suspicious_path(path:str)->str|None:
 norm=path.removeprefix("./")
 if norm.startswith(UNPUBLISHABLE_DIRECTORIES):
  return "local_or_private_directory"
 parts=norm.split("/")
 if parts[-1] in UNPUBLISHABLE_NAMES or (parts[-1].startswith(".env.") and not parts[-1].endswith(".example")):
  return "private_environment_file"
 if norm.lower().endswith(PRIVATE_FILE_SUFFIXES) and not norm.endswith(".example"):
  return "possibly_private_key_or_database"
 return None

def scan_bytes(path:str, data:bytes)->set[str]:
 findings=set()
 flag=suspicious_path(path)
 if flag:
  findings.add(flag)
 if not data or b"\x00" in data[:1024] or len(data)>MAX_FILE_BYTES:
  return findings
 for kind,pattern in PATTERNS.items():
  if pattern.search(data):
   findings.add(kind)
 return findings

def blob_table()->tuple[list[str],dict[str,list[str]],dict[str,int]]:
 lines=run_git("rev-list","--objects","--all").decode("utf-8","replace").splitlines()
 paths:dict[str,list[str]]={}
 for line in lines:
  oid,sep,path=line.partition(" ")
  if len(oid)==40 and all(c in "0123456789abcdef" for c in oid):
   if sep:
    paths.setdefault(oid,[]).append(path)
   else:paths.setdefault(oid,[])
 ids=list(paths)
 if not ids:return [],paths,{}
 raw=run_git("cat-file","--batch-check=%(objectname) %(objecttype) %(objectsize)",
             input_bytes=("\n".join(ids)+"\n").encode())
 info={}
 for line in raw.decode("utf-8","replace").splitlines():
  oid,kind,size=line.split()
  if kind=="blob":
   info[oid]=int(size)
 return ids,paths,info

def read_blobs(ids:list[str],sizes:dict[str,int]):
 chunk=[]
 budget=0
 def unpack(request:list[str]):
  raw=run_git("cat-file","--batch",input_bytes=("\n".join(request)+"\n").encode())
  offset=0
  for requested in request:
   end=raw.find(b"\n",offset)
   if end<0:raise ValueError("Truncated Git object batch header")
   meta=raw[offset:end].decode("ascii").split()
   oid,kind,size=meta[0],meta[1],int(meta[2])
   if oid!=requested or kind!="blob" or size!=sizes[oid]:
    raise ValueError("Unverified Git object content")
   data=raw[end+1:end+1+size]
   if len(data)!=size or raw[end+1+size:end+2+size]!=b"\n":
    raise ValueError("Truncated Git object")
   yield oid,data
   offset=end+2+size
  if offset!=len(raw):raise ValueError("Unexpected trailing Git object output")
 for oid in ids:
  size=sizes.get(oid)
  if size is None or size>MAX_FILE_BYTES:continue
  if budget+size>BATCH_MAX_BYTES or len(chunk)>=45:
   yield from unpack(chunk)
   chunk=[];budget=0
  chunk.append(oid);budget+=size
 if chunk:yield from unpack(chunk)

def audit()->dict:
 ids,paths,sizes=blob_table()
 found=[]
 dedup=set()
 def record(path:str,kind:str,ref:str,origin:str):
  value=(origin,path,kind)
  if value in dedup:return
  dedup.add(value)
  found.append({"origin":origin,"path":path,"category":kind,
                "object_id_prefix":ref[:12] if origin=="git_history" else ""})
 scanned=0
 for oid,data in read_blobs(ids,sizes):
  scanned+=1
  for path in paths.get(oid,[]):
   for kind in scan_bytes(path,data):
    record(path,kind,oid,"git_history")
 # Always inspect the currently checked-out tree, including new untracked
 # user files, without following symlinks or reading ignored local secrets.
 tracked=run_git("ls-files","-z").split(b"\x00")
 untracked=run_git("ls-files","--others","--exclude-standard","-z").split(b"\x00")
 current=[p.decode("utf-8","replace") for p in tracked+untracked if p]
 tree_scanned=0
 for path in sorted(set(current)):
  f=ROOT/path
  if not f.is_file() or f.is_symlink():
   continue
  flag=suspicious_path(path)
  if f.stat().st_size>MAX_FILE_BYTES:
   if flag:record(path,flag,"","current_tree")
   continue
  tree_scanned+=1
  for kind in scan_bytes(path,f.read_bytes()):
   record(path,kind,"","current_tree")
 found.sort(key=lambda r:(r["category"],r["path"],r["origin"]))
 counts={}
 for item in found:
  counts[item["category"]]=counts.get(item["category"],0)+1
 return {
  "schema_version":REPORT_VERSION,
  "status":"RELEASE_PREFLIGHT_ONLY_NOT_SECURITY_OR_LICENSE_CERTIFICATION",
  "commit_count":len(run_git("log","--all","--format=%H").splitlines()),
  "history_blobs_total":len(sizes),
  "history_blobs_scanned":scanned,
  "history_blobs_skipped_too_large":sum(n>MAX_FILE_BYTES for n in sizes.values()),
  "current_tree_files_scanned":tree_scanned,
  "finding_count":len(found),
  "category_counts":dict(sorted(counts.items())),
  "findings":found,
  "secrets_disclosed_in_report":False,
  "limitations":["Heuristic token/string patterns only; no high-entropy comprehensive scanner.",
                 "Read-only current Git refs only; unreachable rewritten/dangling objects not scanned.",
                 "External artifact and third-party license verification not performed.",
                 "A sample token matching a pattern can be a safe test fixture and requires manual validation.",
                 "No Git history rewrite, push, public visibility, database access or deployment."],
 }

def main():
 p=argparse.ArgumentParser(description=__doc__)
 p.add_argument("--output",required=True,type=Path)
 args=p.parse_args()
 out=args.output.resolve()
 local=(ROOT/".local").resolve()
 if not out.is_relative_to(local) or out.exists() or out.is_symlink():
  raise ValueError("Audit output must be a new file under the ignored .local/ directory")
 report=audit()
 out.parent.mkdir(parents=True,exist_ok=True)
 out.write_text(json.dumps(report,ensure_ascii=False,sort_keys=True,indent=2)+"\n",encoding="utf-8")
 print(json.dumps({key:report[key] for key in
   ("status","commit_count","history_blobs_total","history_blobs_scanned",
    "history_blobs_skipped_too_large","current_tree_files_scanned",
    "finding_count","category_counts","secrets_disclosed_in_report")},
   sort_keys=True))

if __name__=="__main__":main()
