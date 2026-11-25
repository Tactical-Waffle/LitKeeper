// AI Rewrite and AI summaries in the story window.
// The buttons are drawn from the state the server sends (data-state), and again after each action or check.
// The story window holds two copies of its content (phone and larger screens), so every copy is drawn.
(function () {
  const AI_BUTTON = 'flex-1 px-3 py-1.5 text-xs font-medium text-violet-600 dark:text-violet-400 hover:text-violet-700 dark:hover:text-violet-300 hover:bg-violet-50 dark:hover:bg-violet-900/20 rounded-md border border-violet-200/60 dark:border-violet-700 transition-all duration-200 disabled:opacity-60 disabled:cursor-not-allowed';
  const PLAIN_BUTTON = 'flex-1 px-3 py-1.5 text-xs font-medium text-slate-600 dark:text-slate-400 hover:text-slate-900 dark:hover:text-slate-200 hover:bg-slate-100 dark:hover:bg-slate-700/50 rounded-md border border-slate-200/60 dark:border-slate-700 transition-all duration-200';
  const timers = {};

  function toast(message, type) {
    if (typeof showToast === 'function') showToast(message, type);
  }

  function makeButton(label, className, title, onClick) {
    const b = document.createElement('button');
    b.type = 'button';
    b.className = className;
    b.textContent = label;
    if (title) b.title = title;
    b.setAttribute('data-requires-online', '');
    b.addEventListener('click', onClick);
    return b;
  }

  function boxes(kind, storyId) {
    return [...document.querySelectorAll(`.story-ai-text[data-kind="${kind}"]`)]
      .filter(box => Number(box.dataset.storyId) === storyId);
  }

  // Lines of status text; the last one is shown as a problem when `warn` is set.
  function setStatus(status, lines, warn) {
    if (!status) return;
    status.replaceChildren();
    const shown = lines.filter(Boolean);
    shown.forEach((line, i) => {
      const span = document.createElement('span');
      span.className = 'block' + (warn && i === shown.length - 1 ? ' text-red-600 dark:text-red-400' : '');
      span.textContent = line;
      status.appendChild(span);
    });
    status.classList.toggle('hidden', !shown.length);
  }

  async function post(url, body) {
    try {
      const response = await fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
      try {
        return await response.json();
      } catch {
        return { success: false, message: `LitKeeper answered with error ${response.status}` };
      }
    } catch {
      return { success: false, message: 'Could not reach LitKeeper' };
    }
  }

  function money(value) { return value ? ` · $${Number(value).toFixed(4)}` : ''; }

  // Check again while a rewrite or summary is queued or running; say so when it ends. Checks slow down while
  // AI work is paused, and back off when LitKeeper does not answer (giving up after about five minutes).
  const failures = {};
  function poll(kind, storyId, delay) {
    clearTimeout(timers[kind]);
    timers[kind] = setTimeout(async () => {
      if (!boxes(kind, storyId).length) return;  // The story window was closed or shows another story.
      try {
        const response = await fetch(`/api/story/${storyId}/ai-${kind}`);
        if (response.status === 404) return;  // The story was deleted.
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const info = await response.json();
        failures[kind] = 0;
        (kind === 'rewrite' ? drawRewrite : drawSummary)(storyId, info);
        if (!(info.running || info.queued)) {
          const failed = !!info.error;
          toast(kind === 'rewrite' ? (failed ? 'AI Rewrite stopped with a problem' : 'AI Rewrite finished')
                                   : (failed ? 'The summary could not be written' : 'Summary ready'), failed ? 'error' : 'success');
        }
      } catch {
        failures[kind] = (failures[kind] || 0) + 1;
        if (failures[kind] > 20) {
          failures[kind] = 0;
          boxes(kind, storyId).forEach(box => setStatus(box.querySelector('.ai-status'),
            ['Lost contact with LitKeeper. Reopen the story to check again.'], true));
          return;
        }
        poll(kind, storyId, Math.min(30000, 3000 * 2 ** Math.min(failures[kind], 4)));
      }
    }, delay || (kind === 'rewrite' ? 5000 : 3000));
  }

  const PAUSED_HINT = 'Press Resume on the Queue page to try again.';

  function drawRewrite(storyId, info) {
    boxes('rewrite', storyId).forEach(container => drawRewriteInto(container, storyId, info));
    if (info.running || info.queued) poll('rewrite', storyId, info.paused ? 15000 : 0);
  }

  function drawRewriteInto(container, storyId, info) {
    const box = container.querySelector('.ai-buttons');
    box.replaceChildren();
    const act = async (action, confirmText) => {
      if (confirmText && !confirm(confirmText)) return;
      const data = await post(`/api/story/${storyId}/ai-rewrite`, { action });
      if (!data.success) { toast(data.message || 'AI Rewrite could not start', 'error'); return; }
      drawRewrite(storyId, data);
      if (action === 'discard') toast('AI rewrite discarded; the reader shows the original', 'success');
    };
    const lines = [];
    if (info.running) {
      const live = info.live || {};
      lines.push(live.total
        ? `AI Rewrite: part ${live.number} (${live.index} of ${live.total})${live.eta_text ? ' · ' + live.eta_text : ''}${money(info.cost)} so far`
        : 'AI Rewrite: starting…');
      if (live.stopping) lines.push('Stopping after this part…');
      else box.appendChild(makeButton('Stop After This Part', PLAIN_BUTTON, 'Parts already rewritten are kept', () => act('stop')));
    } else if (info.queued) {
      lines.push(info.paused ? `AI Rewrite is waiting: AI work is paused. ${info.paused} ${PAUSED_HINT}` : 'AI Rewrite is waiting its turn.');
      box.appendChild(makeButton('Take Out of Queue', PLAIN_BUTTON, '', () => act('remove')));
    } else {
      if (!info.configured) {
        const b = makeButton('AI Rewrite', AI_BUTTON, 'Add an OpenRouter API key in Settings → AI Covers', () => {});
        b.disabled = true;
        box.appendChild(b);
      } else if (info.done < info.total) {
        const left = info.total - info.done;
        const label = info.done ? `Rewrite the Other ${left} ${left === 1 ? 'Part' : 'Parts'}` : 'AI Rewrite';
        box.appendChild(makeButton(label, AI_BUTTON, 'Proofread and copy-edit with AI. The original is kept.',
          () => act('continue', info.done ? '' : 'Rewrite this story with AI? It costs OpenRouter credit for every part. The original text is kept.')));
      }
      if (info.configured && info.done) {
        box.appendChild(makeButton('Rewrite Again', PLAIN_BUTTON, 'Rewrite every part from the original again',
          () => act('redo', 'Rewrite every part again? The current rewrite is replaced.')));
      }
      if (info.done) {
        box.appendChild(makeButton('Discard Rewrite', PLAIN_BUTTON, 'The reader goes back to the original',
          () => act('discard', 'Discard the AI rewrite of this story? The original text is not affected.')));
        lines.push(`${info.done} of ${info.total} ${info.total === 1 ? 'part' : 'parts'} rewritten${money(info.cost)}. The reader opens the rewrite.`);
      }
      if (info.stopped) lines.push(info.stopped);
    }
    const flags = Object.entries(info.flags || {});
    if (flags.length && !info.running) lines.push('Worth checking: ' + flags.map(([n, reasons]) => `part ${n} ${reasons.join(', ')}`).join('; ') + '.');
    const warn = !!info.error && !info.running && !info.queued;
    if (warn) lines.push(info.error);
    setStatus(container.querySelector('.ai-status'), lines, warn);
  }

  function drawSummary(storyId, info) {
    document.querySelectorAll('.ai-summary-text').forEach(text => {
      const body = text.querySelector('.ai-summary-body');
      if (body) body.textContent = info.summary || '';
      text.classList.toggle('hidden', !info.summary);
    });
    // The AI summary replaces the description from Literotica; clearing it brings the description back.
    document.querySelectorAll('.story-description-text').forEach(p => p.classList.toggle('hidden', !!info.summary));
    boxes('summary', storyId).forEach(container => drawSummaryInto(container, storyId, info));
    if (info.running || info.queued) poll('summary', storyId, info.paused ? 15000 : 0);
  }

  function drawSummaryInto(container, storyId, info) {
    const box = container.querySelector('.ai-buttons');
    box.replaceChildren();
    const act = async (action, confirmText) => {
      if (confirmText && !confirm(confirmText)) return;
      const data = await post(`/api/story/${storyId}/ai-summary`, { action });
      if (!data.success) { toast(data.message || 'The summary could not be started', 'error'); return; }
      drawSummary(storyId, data);
    };
    let line = '';
    if (info.running) {
      line = 'Writing a summary…';
    } else if (info.queued) {
      line = info.paused ? `A summary is waiting: AI work is paused. ${info.paused} ${PAUSED_HINT}` : 'A summary is waiting to be written.';
      box.appendChild(makeButton('Take Out of Queue', PLAIN_BUTTON, '', () => act('remove')));
    } else {
      const b = makeButton(info.summary ? 'New Summary' : 'Make Summary', AI_BUTTON,
        info.configured ? 'One or two sentences by AI; a small fraction of a cent' : 'Add an OpenRouter API key in Settings → AI Covers',
        () => act('make'));
      b.disabled = !info.configured;
      box.appendChild(b);
      if (info.summary) box.appendChild(makeButton('Clear Summary', PLAIN_BUTTON, '', () => act('clear', 'Clear the AI summary?')));
    }
    const warn = !!info.error && !info.running && !info.queued;
    setStatus(container.querySelector('.ai-status'), warn ? [info.error] : [line], warn);
  }

  window.initStoryAiText = function () {
    for (const [kind, draw] of [['rewrite', drawRewrite], ['summary', drawSummary]]) {
      const box = document.querySelector(`.story-ai-text[data-kind="${kind}"]`);
      if (!box) continue;
      let state = {};
      try { state = JSON.parse(box.dataset.state || '{}'); } catch { state = {}; }
      draw(Number(box.dataset.storyId), state);
    }
  };
})();

// The story window opens the moment a story is clicked: its cover and title from the library card, with
// "Loading…", until the details arrive (usually at once; slower while the server is busy with downloads).
(function () {
  let placeholderShown = false;

  document.addEventListener('htmx:beforeRequest', event => {
    const card = event.detail.elt;
    if (!card || !card.matches || !card.matches('.story-card[data-story-id]')) return;
    const container = document.getElementById('modal-container');
    if (!container || container.children.length) return;
    const title = card.querySelector('.card-info-title')?.textContent || card.querySelector('img')?.alt || '';
    const cover = card.querySelector('img')?.getAttribute('src') || '';
    container.innerHTML = `
      <div data-placeholder class="fixed top-0 left-0 right-0 modal-bottom-inset z-50 flex items-end md:items-center justify-center bg-black/50 backdrop-blur-sm">
        <div class="w-full md:max-w-4xl md:mx-4 max-h-[88vh] md:max-h-[90vh] bg-white dark:bg-gray-800 rounded-t-2xl md:rounded-xl shadow-2xl overflow-hidden relative">
          <div class="p-5 md:p-8 flex gap-4 md:gap-8">
            <div class="flex-shrink-0 w-20 h-28 md:w-48 md:h-64 rounded-lg overflow-hidden bg-slate-200 dark:bg-slate-700">
              ${cover ? '<img alt="" class="w-full h-full object-cover">' : ''}
            </div>
            <div class="flex-1 min-w-0">
              <h2 class="text-xl md:text-2xl font-bold text-gray-900 dark:text-white mb-3" data-title></h2>
              <p class="flex items-center gap-2 text-sm text-gray-500 dark:text-gray-400">
                <span class="animate-spin rounded-full h-4 w-4 border-2 border-slate-300 dark:border-slate-600 border-t-slate-600 dark:border-t-slate-300"></span>
                Loading…
              </p>
            </div>
          </div>
        </div>
      </div>`;
    container.querySelector('[data-title]').textContent = title;
    if (cover) container.querySelector('img').src = cover;
    container.querySelector('[data-placeholder]').addEventListener('click', e => {
      if (e.target === e.currentTarget && window.closeStoryModal) window.closeStoryModal();
    });
    placeholderShown = true;
  });

  // The real window replaces the placeholder without sliding in (the placeholder has its own ids, so htmx does
  // not carry its attributes over).
  document.addEventListener('htmx:afterSwap', event => {
    if (!placeholderShown || event.detail.target?.id !== 'modal-container') return;
    placeholderShown = false;
    document.getElementById('storyModalSheet')?.classList.remove('modal-sheet-enter');
  });
  // If the details could not be loaded, the placeholder goes away (the card shows the error).
  document.addEventListener('htmx:afterRequest', event => {
    if (!placeholderShown || event.detail.successful) return;
    placeholderShown = false;
    const container = document.getElementById('modal-container');
    if (container?.querySelector('[data-placeholder]')) container.innerHTML = '';
  });
})();

// Update from Literotica in the story window: queue the update, then check every few seconds until it is done
// and show the new numbers. The update has its own queue, separate from downloads (see the Queue page).
(function () {
  let timer = null;

  function draw(storyId, info) {
    document.querySelectorAll(`.literotica-stats[data-story-id="${storyId}"]`).forEach(box => {
      const button = box.querySelector('.literotica-update-btn');
      if (button) {
        button.textContent = info.running ? 'Updating…' : info.queued ? 'Waiting to update…' : 'Update from Literotica';
        button.disabled = !!(info.running || info.queued);
      }
      box.querySelector('.literotica-update-error')?.remove();
      if (info.error && !info.running && !info.queued) {
        const error = document.createElement('span');
        error.className = 'literotica-update-error text-red-600 dark:text-red-400';
        error.textContent = info.error;
        box.appendChild(error);
      }
      const numbers = box.querySelector('.literotica-numbers');
      if (!numbers || info.running || info.queued) return;
      const parts = [];
      const add = (text, title, prefix, prefixClass) => {
        const span = document.createElement('span');
        span.title = title;
        if (prefix) {
          const mark = document.createElement('span');
          mark.className = prefixClass;
          mark.textContent = prefix;
          span.append(mark, ' ');
        }
        span.append(text);
        parts.push(span);
      };
      const n = v => Number(v).toLocaleString('en-US');
      if (info.community_score != null) add(`${Number(info.community_score).toFixed(2)} community`, 'Community rating on Literotica', '★', 'text-amber-500 font-medium');
      if (info.views != null) add(`${n(info.views)} ${info.views === 1 ? 'view' : 'views'}`, `${n(info.views)} views on Literotica`);
      if (info.favorites != null) add(`${n(info.favorites)} ${info.favorites === 1 ? 'favorite' : 'favorites'}`, `${n(info.favorites)} favorites on Literotica`, '♥', 'text-rose-400');
      if (info.comments) add(`${n(info.comments)} ${info.comments === 1 ? 'comment' : 'comments'}`, 'Comments on Literotica');
      numbers.replaceChildren(...parts);
    });
  }

  window.pollLiteroticaUpdate = function (storyId) {
    clearTimeout(timer);
    timer = setTimeout(async () => {
      if (!document.querySelector(`.literotica-stats[data-story-id="${storyId}"]`)) return;  // Window closed.
      try {
        const info = await (await fetch(`/api/story/${storyId}/literotica-update`)).json();
        draw(storyId, info);
        if (info.running || info.queued) window.pollLiteroticaUpdate(storyId);
        else if (typeof showToast === 'function') showToast(info.error ? 'The update from Literotica failed' : 'Updated from Literotica', info.error ? 'error' : 'success');
      } catch {
        window.pollLiteroticaUpdate(storyId);
      }
    }, 3000);
  };

  window.updateFromLiterotica = async function (storyId) {
    let info;
    try {
      info = await (await fetch(`/api/story/${storyId}/literotica-update`, { method: 'POST' })).json();
    } catch {
      info = { success: false, message: 'Could not reach LitKeeper' };
    }
    if (!info.success) {
      if (typeof showToast === 'function') showToast(info.message || 'The update could not be queued', 'error');
      return;
    }
    draw(storyId, info);
    window.pollLiteroticaUpdate(storyId);
  };
})();
