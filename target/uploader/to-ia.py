#!/usr/bin/env python3

import os, os.path, time, subprocess, shutil

IA_UPLOAD_STREAM = shutil.which("ia-upload-stream")
CURL_IA = shutil.which("curl-ia")
IA_WAIT_ITEM_TASKS = shutil.which("ia-wait-item-tasks")
if not all([IA_UPLOAD_STREAM, CURL_IA, IA_WAIT_ITEM_TASKS]):
    raise RuntimeError("One or more of ia-upload-stream, curl-ia, or ia-wait-item-tasks was not found.")

TITLE = os.environ['ITEM_TITLE']
DESCRIPTION = os.environ['ITEM_DESC']
COLLECTION = os.environ['ITEM_COLLECTION']
ROOT = os.environ['ROOT']

def upload_file(ident: str, local_path: str, remote_path: str):
    subprocess.run([
        IA_UPLOAD_STREAM,
        "--input-file", local_path,
        "--part-size", "1024G",
        ident,
        remote_path,
        f"title:{TITLE}",
        f"description:{DESCRIPTION}",
        f"collection:{COLLECTION}"
    ], check=True)
    while True:
        p = subprocess.run([
            IA_WAIT_ITEM_TASKS,
            ident
        ])
        if p.returncode == 0:
            break
        else:
            print(f"ia-wait-item-tasks exited with code {p.returncode}; sleeping 60 seconds")
            time.sleep(60)
    os.remove(local_path)

def upload_directory(dirname: str):
    ident = os.path.basename(dirname)
    for subdirname, _, filenames in os.walk(dirname, topdown = False):
        for filename in filenames:
            # Creates a functional path to the file
            lp = os.path.join(subdirname, filename)
            # Removes the upload root and the identifier dir from the path
            rp = os.path.relpath(lp, dirname)
            print("upload", ident, lp, rp)
            upload_file(ident, lp, rp)
        os.rmdir(subdirname)

uploaded_anything = False
with os.scandir(ROOT) as it:
    for entry in it:
        if entry.is_dir(follow_symlinks = False):
            rfi = os.path.join(entry, ".ready-for-ia")
            if not os.path.isfile(rfi):
                continue
            os.remove(rfi)
            upload_directory(entry.path)
            uploaded_anything = True

if uploaded_anything: print("Scan complete")
