import os
import sys
import subprocess
import uuid
import shutil

if len(sys.argv) != 5:
    print("Usage:")
    print(f"  {sys.argv[0]} <DATA_DIR> <TEMPDIR> <UPLOAD_SERVER> <UPLOADER>")
    sys.exit(1)
_, DATA_DIR, TEMPDIR, UPLOAD_SERVER, UPLOADER = sys.argv

bc = shutil.which("bullseye-client")
if not bc:
    raise RuntimeError("No usable bullseye-client found.")

for ent in os.scandir(DATA_DIR):
    if not ent.is_file(follow_symlinks=False): continue
    if not ent.name.endswith(".warc.gz"): continue
    try:
        mnbot, pipeline, item, ts, seq, rand = ent.name.split("-")
        rand, extension = rand.split(".", 1)
        item = uuid.UUID(item.replace("_", "-"))
    except ValueError:
        print("Skipping file with invalid name", ent.name, flush=True)
        continue
    print("Uploading file", ent.name)
    subprocess.run(
        [
            bc,
            "--project", "mnbot",
            "--pipeline", "warcprox-warc",
            "--uploader", UPLOADER,
            "--base-url", UPLOAD_SERVER,
            ent.path,
            str(item)
         ],
        check=True
    )
    print("Upload succeeded, removing file")
    os.remove(ent.path)
