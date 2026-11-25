(function() {
  const html = document.documentElement;
  const root = document.documentElement;
  const themeLightBtn = document.getElementById('themeLightBtn');
  const themeDarkBtn = document.getElementById('themeDarkBtn');
  const settingsToggle = document.getElementById('settingsToggle');
  const settingsPanel = document.getElementById('settingsPanel');
  const fontSelect = document.getElementById('fontSelect');
  const fontSizeRange = document.getElementById('fontSizeRange');
  const fontSizeValue = document.getElementById('fontSizeValue');
  const lineHeightRange = document.getElementById('lineHeightRange');
  const lineHeightValue = document.getElementById('lineHeightValue');
  const widthRange = document.getElementById('widthRange');
  const widthValue = document.getElementById('widthValue');

  let savedThemePreference = 'system';

  async function fetchThemePreference() {
    try {
      const response = await fetch('/settings/theme-preference');
      const data = await response.json();
      if (data.success) {
        savedThemePreference = data.theme;
        localStorage.setItem('litkeeper_theme', data.theme);
        return data.theme;
      }
    } catch {
      const local = localStorage.getItem('litkeeper_theme');
      if (local) return local;
    }
    return 'system';
  }

  function getSystemTheme() {
    return window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
  }

  function setTheme(theme) {
    html.setAttribute('data-theme', theme === 'dark' ? 'dark' : 'light');
    if (themeLightBtn) themeLightBtn.classList.toggle('active', theme !== 'dark');
    if (themeDarkBtn) themeDarkBtn.classList.toggle('active', theme === 'dark');
  }

  function applyThemePreference(preference) {
    if (preference === 'system') {
      setTheme(getSystemTheme());
    } else {
      setTheme(preference);
    }
    savedThemePreference = preference;
  }

  async function initTheme() {
    const preference = await fetchThemePreference();
    applyThemePreference(preference);
  }

  function saveAndApplyTheme(preference) {
    applyThemePreference(preference);
    localStorage.setItem('litkeeper_theme', preference);
    fetch('/settings/theme-preference', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ theme: preference })
    }).catch(() => {});
  }

  initTheme();
  if (themeLightBtn) themeLightBtn.addEventListener('click', () => saveAndApplyTheme('light'));
  if (themeDarkBtn) themeDarkBtn.addEventListener('click', () => saveAndApplyTheme('dark'));

  window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', (e) => {
    if (savedThemePreference === 'system') {
      setTheme(e.matches ? 'dark' : 'light');
    }
  });

  window.addEventListener('offline', () => { document.documentElement.dataset.offline = ''; });
  window.addEventListener('online',  () => { delete document.documentElement.dataset.offline; });

  window.addEventListener('pageshow', async (event) => {
    if (event.persisted || performance.getEntriesByType('navigation')[0]?.type === 'back_forward') {
      const preference = await fetchThemePreference();
      applyThemePreference(preference);
    }
  });

  // Settings panel toggle
  settingsToggle.addEventListener('click', (e) => {
    e.stopPropagation();
    settingsPanel.classList.toggle('active');
  });

  // Close settings when clicking outside
  document.addEventListener('click', (e) => {
    if (!settingsPanel.contains(e.target) && !settingsToggle.contains(e.target)) {
      settingsPanel.classList.remove('active');
    }
  });

  // Font family selection
  const savedFont = localStorage.getItem('fontFamily') || "ProximaNovaMedium, system-ui, -apple-system, 'Segoe UI', Roboto, Ubuntu, Cantarell, 'Noto Sans', Helvetica, Arial, sans-serif";
  fontSelect.value = savedFont;
  root.style.setProperty('--font-family', savedFont);

  fontSelect.addEventListener('change', (e) => {
    const font = e.target.value;
    root.style.setProperty('--font-family', font);
    localStorage.setItem('fontFamily', font);
  });

  // Font size adjustment
  const savedFontSize = localStorage.getItem('fontSize') || '16';
  fontSizeRange.value = savedFontSize;
  fontSizeValue.textContent = savedFontSize + 'px';
  root.style.setProperty('--font-size', savedFontSize + 'px');

  fontSizeRange.addEventListener('input', (e) => {
    const size = e.target.value;
    fontSizeValue.textContent = size + 'px';
    root.style.setProperty('--font-size', size + 'px');
    localStorage.setItem('fontSize', size);
  });

  // Line height adjustment
  const savedLineHeight = localStorage.getItem('lineHeight') || '1.58';
  lineHeightRange.value = savedLineHeight;
  lineHeightValue.textContent = savedLineHeight;
  root.style.setProperty('--line-height', savedLineHeight);

  lineHeightRange.addEventListener('input', (e) => {
    const height = e.target.value;
    lineHeightValue.textContent = height;
    root.style.setProperty('--line-height', height);
    localStorage.setItem('lineHeight', height);
  });

  // Width adjustment
  const savedWidth = localStorage.getItem('readingWidth') || '800';
  widthRange.value = savedWidth;
  widthValue.textContent = savedWidth + 'px';
  root.style.setProperty('--max-width', savedWidth + 'px');

  widthRange.addEventListener('input', (e) => {
    const width = e.target.value;
    widthValue.textContent = width + 'px';
    root.style.setProperty('--max-width', width + 'px');
    localStorage.setItem('readingWidth', width);
  });

  // Mobile margin adjustment
  const marginRange = document.getElementById('marginRange');
  const marginValue = document.getElementById('marginValue');
  const savedMargin = localStorage.getItem('mobileMargin') || '16';
  marginRange.value = savedMargin;
  marginValue.textContent = savedMargin + 'px';
  root.style.setProperty('--mobile-margin', savedMargin + 'px');

  marginRange.addEventListener('input', (e) => {
    const margin = e.target.value;
    marginValue.textContent = margin + 'px';
    root.style.setProperty('--mobile-margin', margin + 'px');
    localStorage.setItem('mobileMargin', margin);
  });

  function applyMobileVisibility() {
    const mobile = window.innerWidth <= 768;
    document.querySelectorAll('.desktop-only-setting').forEach(el => {
      el.style.display = mobile ? 'none' : '';
    });
    document.querySelectorAll('.mobile-only-setting').forEach(el => {
      el.style.display = mobile ? '' : 'none';
    });
  }
  applyMobileVisibility();
  window.addEventListener('resize', applyMobileVisibility);

  // Auto-hide header on mobile: hides on scroll, reveals on tap
  const readerHeader = document.querySelector('.reader-header');
  const isMobile = window.innerWidth <= 768;

  if (isMobile && readerHeader) {
    let isHidden = false;
    let hasScrolled = false;
    let hideTimeout;

    function showHeader() {
      readerHeader.classList.remove('hidden');
      isHidden = false;
      clearTimeout(hideTimeout);
      if (hasScrolled) {
        hideTimeout = setTimeout(() => {
          if (!settingsPanel.classList.contains('active')) {
            readerHeader.classList.add('hidden');
            isHidden = true;
          }
        }, 3000);
      }
    }

    let lastScrollTop = 0;
    window.addEventListener('scroll', () => {
      const currentScrollTop = window.pageYOffset || document.documentElement.scrollTop;
      if (Math.abs(currentScrollTop - lastScrollTop) < 5) return;

      hasScrolled = true;

      if (currentScrollTop > lastScrollTop && currentScrollTop > 50 && !isHidden) {
        readerHeader.classList.add('hidden');
        settingsPanel.classList.remove('active');
        isHidden = true;
        clearTimeout(hideTimeout);
      }

      lastScrollTop = currentScrollTop;
    }, { passive: true });

    document.addEventListener('click', (e) => {
      if (isHidden && !e.readerPageTurn) showHeader();
    });
  }

  // --- Back to the library -------------------------------------------------------------------
  // Opened from the library, going back returns to it as it was left (its filters and scroll position).
  document.querySelectorAll('[data-library-link]').forEach(link => link.addEventListener('click', event => {
    try {
      const from = new URL(document.referrer);
      if (from.origin === location.origin && from.pathname === '/' && history.length > 1) {
        event.preventDefault();
        history.back();
      }
    } catch (_) { /* No referrer: follow the link. */ }
  }));

  function readPref(name, fallback) {
    try {
      const value = localStorage.getItem(name);
      return value === null ? fallback : value === 'on';
    } catch (_) { return fallback; }
  }
  function writePref(name, on) {
    try { localStorage.setItem(name, on ? 'on' : 'off'); } catch (_) {}
  }

  // --- Parts: one at a time, the contents panel, previous and next ------------------------------
  const parts = [...document.querySelectorAll('.story-content .chapter[data-part]')];
  const contentsPanel = document.getElementById('contentsPanel');
  const contentsToggle = document.getElementById('contentsToggle');
  const partModeToggle = document.getElementById('partModeToggle');
  let partMode = parts.length > 1 && readPref('readerPartMode', true);
  let activePart = parts[0] || null;

  function headerOffset() {
    const header = document.querySelector('.reader-header');
    return header && !header.classList.contains('hidden') ? header.offsetHeight + 8 : 8;
  }

  function markCurrent() {
    const number = activePart?.dataset.part;
    document.querySelectorAll('.contents-list [data-part-link]').forEach(a =>
      a.setAttribute('aria-current', String(a.dataset.partLink === number)));
  }

  function partOf(element) {
    return element?.closest?.('.chapter[data-part]') || null;
  }

  // Show `part` (in one-part-at-a-time mode); returns whether the page changed.
  function showPart(part) {
    if (!part) return false;
    const changed = part !== activePart;
    activePart = part;
    parts.forEach(p => p.classList.toggle('active-part', p === part));
    const index = parts.indexOf(part);
    document.body.dataset.firstPart = String(index === 0);
    document.body.dataset.lastPart = String(index === parts.length - 1);
    markCurrent();
    return changed;
  }

  function scrollToPartStart(part) {
    if (partMode && parts.indexOf(part) === 0) { window.scrollTo({ top: 0, behavior: 'instant' }); return; }
    const top = part.getBoundingClientRect().top + window.scrollY - headerOffset();
    window.scrollTo({ top: Math.max(0, top), behavior: 'instant' });
  }

  function goToPart(part, where = 'start') {
    if (!part) return;
    if (partMode) showPart(part);
    if (where === 'end') {
      const bottom = part.getBoundingClientRect().bottom + window.scrollY;
      window.scrollTo({ top: Math.max(0, bottom - window.innerHeight), behavior: 'instant' });
    } else {
      scrollToPartStart(part);
    }
    activePart = part;
    markCurrent();
    window.dispatchEvent(new Event('scroll'));  // Saves the reading position.
  }

  function applyPartMode(on, keepPlace = true) {
    partMode = on && parts.length > 1;
    document.body.dataset.partMode = partMode ? 'on' : 'off';
    if (partModeToggle) partModeToggle.checked = partMode;
    if (!parts.length) return;
    const anchor = keepPlace ? topParagraph() : null;
    if (partMode) {
      showPart(partOf(anchor) || activePart || parts[0]);
    } else {
      parts.forEach(p => p.classList.remove('active-part'));
      delete document.body.dataset.firstPart;
      delete document.body.dataset.lastPart;
    }
    if (anchor) anchor.scrollIntoView({ behavior: 'instant', block: 'start' });
  }

  // The first paragraph at the top of the screen (only the shown part counts).
  function topParagraph() {
    const scope = partMode && activePart ? activePart : document;
    const paras = scope.querySelectorAll('p[data-chapter]');
    for (const p of paras) {
      if (p.getBoundingClientRect().top >= -10) return p;
    }
    return paras[paras.length - 1] || null;
  }

  // Make a paragraph reachable (its part shown) before scrolling to it.
  function revealParagraph(element) {
    const part = partOf(element);
    if (part) { if (partMode) showPart(part); activePart = part; markCurrent(); }
  }

  function openContents(open) {
    if (!contentsPanel) {
      document.getElementById('table-of-contents')?.scrollIntoView({ behavior: 'instant', block: 'start' });
      return;
    }
    contentsPanel.hidden = !open;
    contentsToggle?.setAttribute('aria-expanded', String(open));
    if (open) {
      markCurrent();
      contentsPanel.querySelector('[aria-current="true"]')?.scrollIntoView({ block: 'nearest' });
    }
  }

  if (parts.length > 1) {
    applyPartMode(partMode, false);
    partModeToggle?.addEventListener('change', () => { writePref('readerPartMode', partModeToggle.checked); applyPartMode(partModeToggle.checked); });
    contentsToggle?.addEventListener('click', event => { event.stopPropagation(); openContents(contentsPanel.hidden); });
    document.addEventListener('click', event => {
      const partLink = event.target.closest('[data-part-link]');
      if (partLink) {
        event.preventDefault();
        openContents(false);
        goToPart(document.getElementById(`chapter-${partLink.dataset.partLink}`));
        return;
      }
      if (event.target.closest('[data-contents-open]')) {
        event.preventDefault();
        event.stopPropagation();
        openContents(true);
        return;
      }
      if (event.target.closest('[data-contents-close]') ||
          (contentsPanel && !contentsPanel.hidden && !contentsPanel.contains(event.target))) {
        openContents(false);
      }
    });
    // The table of contents at the top links to each part too.
    document.querySelectorAll('.toc-link[href^="#chapter-"]').forEach(a => {
      a.dataset.partLink = a.getAttribute('href').slice('#chapter-'.length);
    });
    document.addEventListener('keydown', event => { if (event.key === 'Escape') openContents(false); });
    // Keep the contents panel's current part up to date while scrolling through every part.
    let partTimer = null;
    window.addEventListener('scroll', () => {
      if (partMode) return;
      clearTimeout(partTimer);
      partTimer = setTimeout(() => { const p = partOf(topParagraph()); if (p) { activePart = p; markCurrent(); } }, 200);
    }, { passive: true });
  } else {
    document.body.dataset.partMode = 'off';
  }

  // --- Turning pages: tap the right side to go down a screen, the left side to go back ----------
  // A page is the screen less two lines, so the last lines read stay in view. At the end of a part shown on its
  // own, the next tap goes on to the next part (and back, at its start).
  const tapToggle = document.getElementById('tapPagingToggle');
  let tapPaging = readPref('readerTapPaging', window.matchMedia('(pointer: coarse)').matches);
  function applyTapPaging(on) {
    tapPaging = on;
    document.body.dataset.tapPaging = on ? 'on' : 'off';
    if (tapToggle) tapToggle.checked = on;
  }
  applyTapPaging(tapPaging);
  tapToggle?.addEventListener('change', () => { writePref('readerTapPaging', tapToggle.checked); applyTapPaging(tapToggle.checked); });

  function turnPage(direction) {
    const style = getComputedStyle(document.body);
    const line = (parseFloat(style.lineHeight) || parseFloat(style.fontSize) * 1.6 || 26);
    const header = document.querySelector('.reader-header');
    const covered = header && !header.classList.contains('hidden') ? header.offsetHeight : 0;
    const step = Math.max(100, window.innerHeight - covered - 2 * line);
    const max = document.documentElement.scrollHeight - window.innerHeight;
    if (partMode && activePart) {
      const index = parts.indexOf(activePart);
      if (direction > 0 && window.scrollY >= max - 2 && index < parts.length - 1) { goToPart(parts[index + 1]); return; }
      if (direction < 0 && window.scrollY <= 2 && index > 0) { goToPart(parts[index - 1], 'end'); return; }
    }
    window.scrollBy({ top: direction * step, behavior: 'instant' });
  }

  // Runs before the other click handlers (capture), so a page turn does not also show the top bar.
  window.addEventListener('click', event => {
    if (!tapPaging || event.button !== 0) return;
    if (event.target.closest('a, button, input, select, textarea, label, summary, .reader-header, .settings-panel, ' +
                             '.contents-panel, .rate-story-modal, .story-end-actions, .part-nav, .part-end-nav, .table-of-contents')) return;
    if (settingsPanel.classList.contains('active') || (contentsPanel && !contentsPanel.hidden)) return;
    if (String(window.getSelection?.() || '').trim()) return;  // Selecting text (to save a quote) is not a tap.
    const x = event.clientX / window.innerWidth;
    if (x > 0.3 && x < 0.7) return;  // The middle shows the top bar.
    event.readerPageTurn = true;
    turnPage(x >= 0.7 ? 1 : -1);
  }, true);

  document.addEventListener('keydown', event => {
    if (event.target.closest?.('input, select, textarea') || event.altKey || event.ctrlKey || event.metaKey) return;
    if (event.key === 'ArrowRight') { event.preventDefault(); turnPage(1); }
    else if (event.key === 'ArrowLeft') { event.preventDefault(); turnPage(-1); }
  });

  window.LitKeeperReader = { revealParagraph, topParagraph, parts, isPartMode: () => partMode };


  if (window.STORY_ID) {
    const storyId = window.STORY_ID;
    const initialProgress = window.INITIAL_PROGRESS;

    // Lightweight IndexedDB wrapper — mirrors epub_reader.js ProgressDB,
    // uses the same db/store so both readers share local cache.
    const progressDB = {
      dbName: 'LitKeeperProgress',
      storeName: 'reading_progress',
      _db: null,

      async open() {
        if (this._db) return this._db;
        return new Promise((resolve, reject) => {
          const req = indexedDB.open(this.dbName, 1);
          req.onupgradeneeded = (e) => {
            const db = e.target.result;
            if (!db.objectStoreNames.contains(this.storeName)) {
              db.createObjectStore(this.storeName, { keyPath: 'story_id' });
            }
          };
          req.onsuccess = (e) => { this._db = e.target.result; resolve(this._db); };
          req.onerror = () => reject(req.error);
        });
      },

      async save(data) {
        try {
          const db = await this.open();
          return new Promise((resolve, reject) => {
            const tx = db.transaction(this.storeName, 'readwrite');
            tx.objectStore(this.storeName).put(data);
            tx.oncomplete = resolve;
            tx.onerror = () => reject(tx.error);
          });
        } catch (_) {}
      }
    };

    let pendingSync = false;

    async function saveProgress(chapter, para, scrollPos, pct, paragraphId) {
      const payload = {
        current_chapter: chapter,
        current_paragraph: para,
        scroll_position: scrollPos,
        percentage: pct,
        paragraph_id: paragraphId || null,
        progress_format: 'text'
      };

      // Save locally first (offline resilience)
      await progressDB.save({
        story_id: storyId,
        ...payload,
        timestamp: new Date().toISOString(),
        synced: false
      });

      try {
        const res = await fetch(`/epub/api/progress/${storyId}`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload)
        });
        if (res.ok) {
          pendingSync = false;
        } else {
          pendingSync = true;
        }
      } catch (_) {
        pendingSync = true;
      }
    }

    async function syncPending() {
      if (!pendingSync) return;
      const db = await progressDB.open().catch(() => null);
      if (!db) return;
      const record = await new Promise((resolve) => {
        const tx = db.transaction(progressDB.storeName, 'readonly');
        const req = tx.objectStore(progressDB.storeName).get(storyId);
        req.onsuccess = () => resolve(req.result);
        req.onerror = () => resolve(null);
      });
      if (!record) return;
      try {
        await fetch(`/epub/api/progress/${storyId}`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            current_chapter: record.current_chapter,
            current_paragraph: record.current_paragraph,
            scroll_position: record.scroll_position,
            percentage: record.percentage,
            paragraph_id: record.paragraph_id || null,
            progress_format: 'text'
          })
        });
        pendingSync = false;
      } catch (_) {}
    }

    window.addEventListener('online', syncPending);

    // --- Scroll tracking ---
    const allParas = [...document.querySelectorAll('p[data-chapter]')];
    const getTopVisibleParagraph = () => window.LitKeeperReader.topParagraph();

    // How far through the whole story: by paragraph when one part is shown at a time (the page is then one part).
    function storyPercentage(p) {
      const scrollable = document.documentElement.scrollHeight - window.innerHeight;
      const pagePct = scrollable > 0 ? Math.min(1, window.scrollY / scrollable) : 0;
      if (!window.LitKeeperReader.isPartMode() || !p || !allParas.length) return pagePct;
      const index = allParas.indexOf(p);
      const end = document.documentElement.scrollHeight - window.innerHeight;
      if (window.scrollY >= end - 2 && document.body.dataset.lastPart === 'true') return 1;
      return index < 0 ? pagePct : Math.min(1, index / allParas.length);
    }

    let scrollTimer = null;
    window.addEventListener('scroll', () => {
      clearTimeout(scrollTimer);
      scrollTimer = setTimeout(() => {
        const p = getTopVisibleParagraph();
        const pct = storyPercentage(p);
        const chapter = p ? parseInt(p.dataset.chapter, 10) : 1;
        const para = p ? parseInt(p.dataset.para, 10) : 0;
        const paragraphId = p ? p.id : null;
        saveProgress(chapter, para, Math.round(window.scrollY), pct, paragraphId);
      }, 1500);
    }, { passive: true });

    // --- Restore position on load ---
    // Compare timestamps: prefer whichever source is newer (local IndexedDB or server).
    // This ensures that progress made on iOS (stored on server) takes precedence over
    // a stale local IndexedDB entry from a previous web session.
    async function restorePosition() {
      let progress = initialProgress;

      try {
        const db = await progressDB.open();
        const local = await new Promise((resolve) => {
          const tx = db.transaction(progressDB.storeName, 'readonly');
          const req = tx.objectStore(progressDB.storeName).get(storyId);
          req.onsuccess = () => resolve(req.result);
          req.onerror = () => resolve(null);
        });
        if (local?.timestamp) {
          if (progress?.last_read_at) {
            // Use whichever is newer
            if (new Date(local.timestamp) >= new Date(progress.last_read_at)) {
              progress = local;
            }
            // else keep server data (e.g. iOS read further)
          } else {
            progress = local;
          }
        }
      } catch (_) {}

      if (!progress) return;

      const {
        paragraph_id: paragraphId,
        current_chapter: chapter,
        current_paragraph: para,
        percentage: pct,
        progress_format: format,
      } = progress;

      // Chapter/paragraph/paragraph_id are only meaningful when this reader (or a
      // prior session of it) wrote them last. `current_chapter` defaults to 1 in
      // the database even for rows the Android (CFI-based) app created, so without
      // this check a story read only on Android would wrongly "resume" at the very
      // start of chapter 1 instead of respecting its real, percentage-based progress.
      const chapterDataIsTrustworthy = format === 'text';

      // Priority 1: global paragraph ID (synced from iOS or saved by this reader)
      if (chapterDataIsTrustworthy && paragraphId) {
        const el = document.getElementById(paragraphId);
        if (el) {
          window.LitKeeperReader.revealParagraph(el);
          el.scrollIntoView({ behavior: 'instant', block: 'start' });
          return;
        }
      }

      // Priority 2: chapter + paragraph index (HTML reader coordinate)
      if (chapterDataIsTrustworthy && chapter && chapter > 0) {
        const target = document.querySelector(`[data-chapter="${chapter}"][data-para="${para}"]`);
        if (target) {
          window.LitKeeperReader.revealParagraph(target);
          target.scrollIntoView({ behavior: 'instant', block: 'start' });
          return;
        }
      }

      // Priority 3: percentage fallback (works cross-format)
      if (pct && pct > 0 && window.LitKeeperReader.isPartMode() && allParas.length) {
        const target = allParas[Math.min(allParas.length - 1, Math.floor(pct * allParas.length))];
        window.LitKeeperReader.revealParagraph(target);
        target.scrollIntoView({ behavior: 'instant', block: 'start' });
        return;
      }
      if (pct && pct > 0) {
        const scrollable = document.documentElement.scrollHeight - window.innerHeight;
        if (scrollable > 0) {
          window.scrollTo({ top: scrollable * pct, behavior: 'instant' });
        }
      }
    }

    // --- Navigate to specific paragraph (from highlights) ---
    if (typeof window.TARGET_CHAPTER !== 'undefined' && typeof window.TARGET_PARA !== 'undefined') {
      const ch1based = window.TARGET_CHAPTER + 1;
      requestAnimationFrame(() => requestAnimationFrame(() => {
        const target = document.querySelector(`[data-chapter="${ch1based}"][data-para="${window.TARGET_PARA}"]`);
        if (target) {
          window.LitKeeperReader.revealParagraph(target);
          target.scrollIntoView({ behavior: 'instant', block: 'center' });
          target.classList.add('highlight-flash');
          setTimeout(() => target.classList.remove('highlight-flash'), 3500);
        }
      }));
    } else {
      // Only restore reading position when not jumping to a specific quote
      requestAnimationFrame(() => requestAnimationFrame(restorePosition));
    }

    // --- Shared star rating helper ---
    function initStarWidget(container, initialRating, onRate) {
      const stars = container.querySelectorAll('[data-value]');
      let activeRating = initialRating;

      function updateStars(rating) {
        stars.forEach(s => s.classList.toggle('active', parseInt(s.dataset.value, 10) <= rating));
      }
      updateStars(activeRating);

      stars.forEach(star => {
        star.addEventListener('mouseenter', () => updateStars(parseInt(star.dataset.value, 10)));
        star.addEventListener('mouseleave', () => updateStars(activeRating));
        star.addEventListener('click', async () => {
          const val = parseInt(star.dataset.value, 10);
          const newRating = val === activeRating ? null : val;
          activeRating = newRating || 0;
          updateStars(activeRating);
          onRate(newRating);
        });
      });
    }

    async function postRating(rating) {
      sessionStorage.setItem('litkeeper_reload', '1');
      try {
        await fetch(`/api/story/${storyId}/rating`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ rating })
        });
      } catch (_) {}
    }

    // --- Header rating widget ---
    const headerWidget = document.getElementById('header-rating-widget');
    if (headerWidget) {
      initStarWidget(headerWidget, window.CURRENT_RATING || 0, postRating);
    }

    // --- Rate story modal ---
    let rateStoryModalInited = false;
    function showRateStoryModal() {
      const modal = document.getElementById('rate-story-modal');
      if (!modal) return;

      if (!rateStoryModalInited) {
        rateStoryModalInited = true;
        const starsContainer = modal.querySelector('.rate-story-modal-stars');
        initStarWidget(starsContainer, window.CURRENT_RATING || 0, (rating) => {
          postRating(rating);
          setTimeout(() => modal.classList.remove('active'), 400);
        });
        document.getElementById('rate-story-modal-overlay')
          ?.addEventListener('click', () => modal.classList.remove('active'));
      }

      modal.classList.add('active');
    }

    const rateStoryBtn = document.getElementById('rate-story-btn');
    if (rateStoryBtn) {
      rateStoryBtn.addEventListener('click', showRateStoryModal);
    }

    // --- Save quote ---
    function showQuoteToast(message) {
      const toast = document.createElement('div');
      toast.textContent = message;
      toast.className = 'quote-toast';
      document.body.appendChild(toast);
      requestAnimationFrame(() => toast.classList.add('quote-toast--visible'));
      setTimeout(() => {
        toast.classList.remove('quote-toast--visible');
        setTimeout(() => toast.remove(), 300);
      }, 2200);
    }

    const saveQuoteBtn = document.getElementById('saveQuoteBtn');
    if (saveQuoteBtn) {
      saveQuoteBtn.addEventListener('click', async () => {
        const sel = window.getSelection();
        const text = sel?.toString().trim();
        if (!text) { showQuoteToast('Select text within the story first'); return; }

        const anchorNode = sel.anchorNode?.nodeType === Node.TEXT_NODE
          ? sel.anchorNode.parentElement : sel.anchorNode;
        const para = anchorNode?.closest('[data-para]');
        if (!para) { showQuoteToast('Select text within a paragraph'); return; }

        const chapterNum = parseInt(para.dataset.chapter, 10); // 1-based in HTML
        const chapterIndex = chapterNum - 1;                    // 0-based for DB
        const paragraphIndex = parseInt(para.dataset.para, 10);

        try {
          const resp = await fetch('/api/highlights', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
              story_id: storyId,
              chapter_index: chapterIndex,
              paragraph_index: paragraphIndex,
              quote_text: text
            })
          });
          if (resp.ok) {
            const result = await resp.json();
            sel.removeAllRanges();
            showQuoteToast(result.duplicate ? 'Already saved' : 'Quote saved');
          } else {
            showQuoteToast('Could not save quote');
          }
        } catch (_) {
          showQuoteToast('Could not save quote');
        }
      });
    }
  }

})();
