use std::{env, fs::{create_dir, remove_file, File}, io::{self, Seek, Write}, os::fd::AsRawFd, path::PathBuf, thread::sleep, time::Duration};

use anyhow::{bail, Context, Result};
use chrono::{Datelike, Utc};
use common::{acquire_lock, db::{DatabaseHandle, UploadRow}, helpers::{MegawarcMetadata, MegawarcTarget}};
use futures::executor::block_on;
use rand::{seq::SliceRandom, thread_rng};

fn make_datestamp() -> String {
    let current_date = Utc::now();
    format!(
        "{}{:02}{:02}",
        current_date.year(),
        current_date.month(),
        current_date.day()
    )
}

const CHROMEBOT_PREFIX: &str = "chromebot-brozzler-";

fn make_dirname(base_dir: &String, current_date: String) -> PathBuf {
    let alphabet: Vec<char> = ('a'..='z').chain('0'..='9').collect();
    let dirname = format!(
        "{CHROMEBOT_PREFIX}{current_date}-{}",
        alphabet.choose_multiple(&mut thread_rng(), 8).collect::<String>()
    );
    [base_dir, &dirname].iter().collect()
}

fn handle_item(conn: &DatabaseHandle, mut item: UploadRow, is_reclaim: bool, dirname: PathBuf) -> Result<()> {
    let fp: PathBuf = [item.dir(), item.id()].iter().collect();
    let mut input = File::open(&fp).context("Failed to open input file")?;
    if let Err(e) = acquire_lock(input.as_raw_fd(), true) {
        eprintln!("couldn't lock: {e}");
        if is_reclaim {
            // Well this is awkward
            bail!("stolen item was actually still being worked on, whoops")
        }
        // Well this is REALLY awkward
        bail!("failed lock is probably concerning")
    }
    let warc_path = dirname.join("chromebot.warc.gz");
    let json_path = dirname.join("chromebot.json");

    let mut warc = File::options()
        .write(true)
        .create(true)
        .truncate(false)
        .open(warc_path)
        .context("Failed to open megawarc")?;
    let warc_offset = warc.seek(std::io::SeekFrom::End(0)).context("Failed to seek to end of megawarc")?;

    let mut json = File::options()
        .append(true)
        .create(true)
        .open(json_path)
        .context("Failed to open JSON")?;
    let json_offset = json.stream_position().context("Failed to get stream position")?;

    let mw_metadata = MegawarcMetadata {
        target: MegawarcTarget {
            container: common::helpers::MegawarcLocation::Warc,
            offset: warc_offset,
            size: item.file().size,
        },
        upload_details: Some(item.clone()),
    };
    let mw_metadata_str = serde_json::to_string(&mw_metadata).context("Failed to serialize row metadata")? + "\n";
    match io::copy(&mut input, &mut warc).and_then(|_| warc.sync_all()) {
        Ok(_) => (),
        Err(e) => {
            eprintln!("error while copying WARC to megawarc: {e:?}, rolling back");
            // If this fails, we can't trust the megawarc anymore, so unwrap.
            warc.set_len(warc_offset).unwrap();
            bail!("error while copying WARC to megawarc: {e:?}");
        }
    };
    match json.write_all(mw_metadata_str.as_bytes()).and_then(|_| json.sync_all()) {
        Ok(_) => (),
        Err(e) => {
            eprintln!("error while writing metadata to JSON: {e:?}, rolling back");
            // If either of these fail, we can't trust the megawarc anymore, so unwrap.
            warc.set_len(warc_offset).unwrap();
            json.set_len(json_offset).unwrap();
            bail!("error while writing JSON metadata: {e:?}");
        }
    };
    block_on(item.change_status(conn, common::db::Status::Finished)).context("Failed to change status")?;
    remove_file(fp)?;
    Ok(())
}

fn do_a_file(conn: &DatabaseHandle, dirname: PathBuf) -> Result<()> {
    let project = "chromebot".to_string();
    let pipeline = "warcprox-warc".to_string();
    let item = block_on(UploadRow::check_out(conn, project.clone(), pipeline.clone(), common::db::Status::Packing, false))?;
    if let Some(item) = item {
        handle_item(conn, item, false, dirname)
    } else {
        let item = block_on(UploadRow::check_out(conn, project, pipeline, common::db::Status::Packing, true))?;
        if let Some(item) = item {
            eprintln!("stealing item {}", item.id());
            handle_item(conn, item, true, dirname)
        } else {
            // TODO: Use changefeeds instead of polling
            sleep(Duration::from_secs(15));
            Ok(())
        }
    }
}

fn main() {
    let args: Vec<String> = env::args().collect();
    assert_eq!(args.len(), 2);
    let base_dir = &args[1];
    let pool = DatabaseHandle::new().unwrap();
    loop {
        let current_date = make_datestamp();
        let dirname = make_dirname(base_dir, current_date.clone());
        create_dir(&dirname).unwrap();
        loop {
            let new_date = make_datestamp();
            if new_date != current_date {
                // Mark directory as ready for upload
                let mut f = dirname.clone();
                f.push(".ready-for-ia");
                File::create_new(f).unwrap();
                break;
            }
            match do_a_file(&pool, dirname.clone()) {
                Ok(()) => (),
                Err(e) => {
                    eprintln!("error: {e}");
                    sleep(Duration::from_secs(30));
                },
            }
        }
    }
}
