// /setup: choose photos for the photo tile in Google Photos' picker, then copy them page by page.

const button = document.getElementById("pick-photos");
const status = document.getElementById("photo-status");

function say(text, bad = false) {
  status.hidden = false;
  status.textContent = text;
  status.className = bad ? "notice bad" : "";
}

async function call(method, path, body) {
  const resp = await fetch(path, {
    method, headers: { "Content-Type": "application/json" }, body: body && JSON.stringify(body),
  });
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) throw new Error((data.error && data.error.message) || `Error ${resp.status}`);
  return data;
}

const wait = (ms) => new Promise((done) => setTimeout(done, ms));

if (button) {
  button.addEventListener("click", async () => {
    // Open the tab now, while the click still counts: browsers block pop-ups opened later.
    const tab = window.open("", "_blank");
    button.disabled = true;
    try {
      const session = await call("POST", "/setup/photos/start");
      if (tab) tab.location = session.pickerUri;
      else say("Allow pop-ups for this page, or open: " + session.pickerUri);
      say("Choose photos in the Google Photos tab, then press Done there…");
      const until = Date.now() + 30 * 60_000;
      while (!(await call("GET", `/setup/photos/${session.id}`)).chosen) {
        if (Date.now() > until) throw new Error("No photos were chosen within 30 minutes. Start again.");
        await wait(session.pollSeconds * 1000);
      }
      let page = "";
      let added = 0;
      do {
        const result = await call("POST", `/setup/photos/${session.id}/copy`, { page });
        added += result.added;
        page = result.next;
        document.getElementById("photo-count").textContent = result.total;
        say(`Copying… ${added} added so far.`);
      } while (page);
      await call("POST", `/setup/photos/${session.id}/done`).catch(() => {});
      say(`Done: ${added} photos added. The dashboard picks them up within half an hour, or straight away after a tap on the clock.`);
    } catch (err) {
      if (tab && !tab.closed && tab.location.href === "about:blank") tab.close();
      say(err.message, true);
    } finally {
      button.disabled = false;
    }
  });
}
