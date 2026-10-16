#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Check tracked content/history for private deployment data, without echoing it."""
import argparse
import ipaddress
import re
import subprocess
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]

def git(*args):return subprocess.check_output(['git',*args],cwd=ROOT)

def violations(data,deny):
    text=data.decode('utf-8',errors='replace');issues=[]
    if any(value and value in text for value in deny):issues.append('private-denylist-match')
    if re.search(r'/Users/[A-Za-z0-9_.-]+|/home/(?!agent(?:/|\b))[A-Za-z0-9_.-]+',text):issues.append('personal-path')
    for match in re.finditer(r'(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w.])',text):
        value=match.group(0)
        line_prefix=text[text.rfind('\n',0,match.start())+1:match.start()]
        if re.fullmatch(r'[A-Za-z0-9_.\[\],-]+==',line_prefix):continue  # Four-part package versions are not IP addresses.
        try:address=ipaddress.ip_address(value)
        except ValueError:continue
        documentation=any(address in ipaddress.ip_network(net) for net in ['192.0.2.0/24','198.51.100.0/24','203.0.113.0/24'])
        if not (address.is_loopback or address.is_unspecified or documentation):issues.append('non-example-ip');break
    for value in re.findall(r'[A-Za-z0-9_.+%-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}',text):
        domain=value.rsplit('@',1)[-1].lower()
        if domain not in ('example.com','example.org','example.net'):issues.append('non-example-email');break
    return sorted(set(issues))

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--history',action='store_true');p.add_argument('--deny-file',type=Path)
    a=p.parse_args();deny=a.deny_file.read_text().splitlines() if a.deny_file else []
    entries=[]
    if a.history:
        objects=git('rev-list','--objects','--all').decode().splitlines()
        for item in objects:
            oid,_,path=item.partition(' ')
            kind=git('cat-file','-t',oid).strip()
            if kind in (b'blob',b'commit'):entries.append((oid,path,git('cat-file','-p',oid)))
    else:
        for path in git('ls-files').decode().splitlines():
            full=ROOT/path
            if full.is_file():entries.append(('worktree',path,full.read_bytes()))
    found=[]
    for oid,path,data in entries:
        issues=violations(data,deny)
        if path and any(part in ('evidence','secrets','runtime') for part in Path(path).parts):issues.append('private-artifact-path')
        if issues:found.append((oid,path,','.join(issues)))
    for oid,path,reason in found:print(f'{oid[:12]} {path or "commit metadata"}: {reason}')
    print(f'Public-data scan: {len(entries)} objects checked; {len(found)} violations')
    raise SystemExit(bool(found))

if __name__=='__main__':main()
