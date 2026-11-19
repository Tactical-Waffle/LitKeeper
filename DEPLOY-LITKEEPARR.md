# Deploying "litkeeparr" on your Docker server

These steps build a Docker image called **litkeeparr** from your local `litkeeparr` branch, then
run it on your server. That branch is LitKeeper plus the new Settings → Notifications page, with the
folders inside the container renamed from `/litkeeper` to `/litkeeparr`.

Nothing here touches GitHub. The code goes straight from this Mac to your server.

**Before you start, replace these placeholders in the commands:**
- `you@your-server`: how you normally connect to the server with `ssh`, for example `chris@192.168.1.20`.
- Port `5000`: change the left-hand number in step 3 if something on the server already uses port 5000.

The commands assume you can reach the server with `ssh` and it has Docker with
`docker compose`. If you manage it through a web panel (Portainer, Unraid, Synology and so on),
the same idea applies: copy the folder over, then create a stack from the compose file in step 3.

---

## 1. Package the code (on this Mac)

In Terminal, go to the LitKeeper folder:

```bash
cd ~/Projects/Codex/LitKeeper
```

Make a zipped copy of the committed code on the `litkeeparr` branch. It leaves out
anything not committed, such as test data and draft notes:

```bash
git archive --format=tar.gz -o ~/Desktop/litkeeparr.tar.gz litkeeparr
```

You now have `litkeeparr.tar.gz` on your Desktop.

## 2. Copy it to the server and unpack it

Copy the file over:

```bash
scp ~/Desktop/litkeeparr.tar.gz you@your-server:~/
```

Log in to the server:

```bash
ssh you@your-server
```

**The rest of the commands run on the server.** Make a folder for litkeeparr, with the code in
a `src` subfolder:

```bash
mkdir -p ~/litkeeparr/src ~/litkeeparr/data ~/litkeeparr/stories
```

```bash
tar -xzf ~/litkeeparr.tar.gz -C ~/litkeeparr/src
```

## 3. Create the compose file

```bash
nano ~/litkeeparr/docker-compose.yml
```

Paste this in, then save with **Ctrl+O**, **Enter**, **Ctrl+X**:

```yaml
services:
  litkeeparr:
    build: ./src                 # build the image from your copy of the code
    image: litkeeparr:latest     # ...and call the image "litkeeparr"
    container_name: litkeeparr
    restart: unless-stopped
    ports:
      - "5000:5000"              # change the LEFT number if port 5000 is taken
    volumes:
      - ./data:/litkeeparr/app/data        # database and secret key
      - ./stories:/litkeeparr/app/stories  # EPUBs, reader files, covers
    environment:
      - ENABLE_LIBRARY=true
```

The right-hand side of each volume line must say `/litkeeparr/...`, to match the folder names inside
this image. The official LitKeeper image uses `/litkeeper/...` instead.

Notifications don't need anything in this file. You set them up in the app afterwards (step 5).

## 4. Build and start it

```bash
cd ~/litkeeparr
```

```bash
docker compose up -d --build
```

The first build takes a few minutes. Watch it start up:

```bash
docker compose logs -f
```

Wait until you see `Starting application...` and the Gunicorn lines, then press **Ctrl+C** to stop
watching. The app keeps running.

## 5. Open it and set up notifications

Go to `http://<server-ip>:5000` in your browser, then **Settings → Notifications → Add Notification**:
1. Choose **Pushover**.
2. Paste your **User Key** and **API Token** (your application's token from pushover.net).
3. Press **Test**. A message should arrive on your phone.
4. Press **Save**.

---

## Already running the official LitKeeper on this server?

You can bring your existing library across. Don't run both on the same data folders at the same
time: they share one database file, and two apps writing to it at once can damage it.

1. Stop the old one (in its own folder): `docker compose down`
2. Copy its data into the new folders. Replace `/path/to/old` with the folder holding its
   `data` and `stories` folders:
   ```bash
   cp -a /path/to/old/data/. ~/litkeeparr/data/
   ```
   ```bash
   cp -a /path/to/old/stories/. ~/litkeeparr/stories/
   ```
3. Start litkeeparr (step 4). The old folders stay untouched as a backup.

This change doesn't alter the database layout, so you can switch back to the official image later
with the same data.

## Updating litkeeparr later

When there are new commits on the branch, repeat step 1 and the `scp` in step 2 on the Mac.
Then on the server:

```bash
rm -rf ~/litkeeparr/src && mkdir ~/litkeeparr/src
```

```bash
tar -xzf ~/litkeeparr.tar.gz -C ~/litkeeparr/src
```

```bash
cd ~/litkeeparr && docker compose up -d --build
```

Your `data` and `stories` folders aren't touched by this.

## Going back to the official LitKeeper

In `~/litkeeparr/docker-compose.yml`:
1. Delete the `build: ./src` line.
2. Change the image line to `image: ghcr.io/redwoodstory/litkeeper:latest`.
3. Change `/litkeeparr/` back to `/litkeeper/` in both volume lines. The official image uses the old folder names.

Then run:

```bash
cd ~/litkeeparr && docker compose up -d
```

Your library and settings stay. Saved notification entries stay in the database but aren't used
by the official version. That version only reads `NOTIFICATION_URLS`.
