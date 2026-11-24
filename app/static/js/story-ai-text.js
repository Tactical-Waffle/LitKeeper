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
      return await response.json();
    } catch {
      return { success: false, message: 'Could not reach LitKeeper' };
    }
  }

  function money(value) { return value ? ` · $${Number(value).toFixed(4)}` : ''; }

  // Check again while a rewrite or summary is queued or running; say so when it ends.
  function poll(kind, storyId) {
    clearTimeout(timers[kind]);
    timers[kind] = setTimeout(async () => {
      if (!boxes(kind, storyId).length) return;  // The story window was closed or shows another story.
      try {
        const info = await (await fetch(`/api/story/${storyId}/ai-${kind}`)).json();
        (kind === 'rewrite' ? drawRewrite : drawSummary)(storyId, info);
        if (!(info.running || info.queued)) {
          const failed = !!info.error;
          toast(kind === 'rewrite' ? (failed ? 'AI Rewrite stopped with a problem' : 'AI Rewrite finished')
                                   : (failed ? 'The summary could not be written' : 'Summary ready'), failed ? 'error' : 'success');
        }
      } catch {
        poll(kind, storyId);
      }
    }, kind === 'rewrite' ? 5000 : 3000);
  }

  function drawRewrite(storyId, info) {
    boxes('rewrite', storyId).forEach(container => drawRewriteInto(container, storyId, info));
    if (info.running || info.queued) poll('rewrite', storyId);
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
      lines.push('AI Rewrite is waiting its turn.');
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
    if (info.running || info.queued) poll('summary', storyId);
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
      line = 'A summary is waiting to be written.';
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
