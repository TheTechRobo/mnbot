use std::{fs::remove_file, os::fd::AsRawFd, panic::catch_unwind, path::PathBuf, process::{Command, Stdio}, thread::sleep, time::Duration};
use futures::executor::block_on;
use anyhow::{bail, Result};
use common::{acquire_lock, db::{self, DatabaseHandle, Status, UploadError, UploadRow}, hash_file};
use std::fs::File;

fn verify_file(file: &File, fp: &PathBuf, file_metadata: &db::File) -> Result<Status> {
    let hash = hash_file(file)?;
    if hash != file_metadata.hash {
        remove_file(fp)?;
        return Ok(Status::Error(UploadError::Checksum));
    }
    let mut zstdcat = Command::new("zstdcat")
        .arg(fp)
        .stdout(Stdio::piped())
        .spawn()?;
    let warc_tiny = Command::new("warc-tiny")
        .arg("verify")
        .arg("-")
        .stdin(zstdcat.stdout.take().unwrap())
        .status()?;

    if !warc_tiny.success() {
        eprintln!("warctiny failed with code {}", warc_tiny);
        remove_file(fp)?;
        return Ok(Status::Error(UploadError::Verify));
    }
    let zs = zstdcat.wait()?;
    if !zs.success() {
        eprintln!("zstdcat failed with code {}", zs);
        remove_file(fp)?;
        return Ok(Status::Error(UploadError::Verify));
    }

    // No deriving for this pipeline
    Ok(Status::Packing)
}

fn handle_item(conn: &DatabaseHandle, mut item: UploadRow, is_reclaim: bool) -> Result<()> {
    let fp: PathBuf = [item.dir(), item.id()].iter().collect();
    let file = File::open(&fp)?;
    if let Err(e) = acquire_lock(file.as_raw_fd(), true) {
        eprintln!("couldn't lock: {e}");
        if is_reclaim {
            // Well this is awkward
            bail!("stolen item was actually still being worked on, whoops")
        }
        // Well this is REALLY awkward
        bail!("failed lock is probably concerning")
    }
    
    let id = item.id().clone();
    let res = catch_unwind(|| verify_file(&file, &fp, item.file()));
    if let Ok(Ok(s)) = res {
        block_on(item.change_status(&conn, s))?;
        Ok(())
    } else {
        let _ = remove_file(fp);
        block_on(item.change_status(&conn, Status::Error(UploadError::Verify)))?;
        bail!("verification failed for item {id}: {res:?}")
    }
}

fn do_one(conn: &DatabaseHandle) -> Result<()> {
    let item = block_on(UploadRow::check_out(conn, "mnbot".to_string(), "warcprox-warc".to_string(), Status::Verifying, false))?;
    if let Some(item) = item {
        handle_item(conn, item, false)
    } else {
        let item = block_on(UploadRow::check_out(conn, "mnbot".to_string(), "warcprox-warc".to_string(), Status::Verifying, true))?;
        if let Some(item) = item {
            eprintln!("stealing item {}", item.id());
            handle_item(conn, item, true)
        } else {
            // TODO: Use changefeeds instead of polling
            sleep(Duration::from_secs(15));
            Ok(())
        }
    }
}

fn main() -> ! {
    let conn = DatabaseHandle::new().unwrap();
    loop {
        let res = do_one(&conn);
        if res.is_err() {
            eprintln!("unhandled error: {res:?}, waiting 5 seconds");
            sleep(Duration::from_secs(5));
        }
    }
}
