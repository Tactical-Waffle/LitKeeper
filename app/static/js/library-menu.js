// Right-click (or the menu key) on a story in the library, as in RedKeeper: a short menu of the common actions —
// Show Details, Read, Mark as Read or Unread, and the reading queue. Arrow keys move through it; Escape closes it.
(function () {
  let menu = null;
  let opener = null;

  function closeMenu() {
    menu?.remove();
    menu = null;
  }

  function toast(message, type) {
    if (typeof showToast === 'function') showToast(message, type);
  }

  // The library follows a change: the card is redrawn, or the whole list when a filter or the queue view depends on it.
  function libraryFollows(storyId) {
    const readFilter = document.getElementById('readStatusFilter');
    const queueOnly = document.getElementById('queueOnlyInput');
    if (((readFilter && readFilter.value) || (queueOnly && queueOnly.value === 'true')) && typeof refreshLibrary === 'function') refreshLibrary();
    else if (typeof refreshLibraryCard === 'function') refreshLibraryCard(storyId);
  }

  async function setRead(storyId, read) {
    try {
      const response = await fetch(`/api/story/${storyId}/read`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ read }),
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok || !data.success) throw new Error();
      if (typeof data.in_queue === 'boolean' && typeof updateLibraryCardQueueBadge === 'function') updateLibraryCardQueueBadge(storyId, data.in_queue);
      toast(data.read ? 'Marked as read' : 'Marked as unread', 'success');
      libraryFollows(storyId);
    } catch {
      toast('Could not change whether the story is read', 'error');
    }
  }

  async function setQueued(storyId, inQueue) {
    try {
      const response = await fetch(`/api/story/${storyId}/queue`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ in_queue: inQueue }),
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok || typeof data.in_queue !== 'boolean') throw new Error();
      if (typeof updateLibraryCardQueueBadge === 'function') updateLibraryCardQueueBadge(storyId, data.in_queue);
      if (typeof updateQueueCountBadge === 'function') updateQueueCountBadge(data.queue_count);
      toast(data.in_queue ? 'Added to reading queue' : 'Removed from reading queue', 'success');
      libraryFollows(storyId);
    } catch {
      toast('Failed to update reading queue', 'error');
    }
  }

  document.addEventListener('contextmenu', event => {
    const card = event.target.closest('#library .story-card[data-story-id]');
    if (!card || event.shiftKey) return;  // Shift + right-click: the browser's own menu.
    event.preventDefault();
    closeMenu();
    opener = card;
    const id = card.dataset.storyId;
    const read = card.dataset.read === '1';
    const queued = card.dataset.inQueue === '1';
    const items = [['details', 'Show Details']];
    if (card.dataset.reader) items.push(['read', 'Read']);
    items.push(['hr'], ['mark', read ? 'Mark as Unread' : 'Mark as Read'],
               ['queue', queued ? 'Remove from Reading Queue' : 'Add to Reading Queue']);
    menu = document.createElement('div');
    menu.className = 'story-menu';
    menu.setAttribute('role', 'menu');
    menu.setAttribute('aria-label', card.querySelector('.card-info-title')?.textContent || 'Story');
    for (const [action, label] of items) {
      if (action === 'hr') { menu.append(document.createElement('hr')); continue; }
      const button = document.createElement('button');
      button.type = 'button';
      button.setAttribute('role', 'menuitem');
      button.dataset.menu = action;
      button.textContent = label;
      menu.append(button);
    }
    menu.addEventListener('click', async click => {
      const action = click.target.closest('[data-menu]')?.dataset.menu;
      if (!action) return;
      closeMenu();
      if (action === 'details') htmx.ajax('GET', `/api/story/${id}/modal`, { target: '#modal-container', swap: 'innerHTML' });
      if (action === 'read') location.href = card.dataset.reader;
      if (action === 'mark') await setRead(id, !read);
      if (action === 'queue') await setQueued(id, !queued);
    });
    menu.addEventListener('keydown', key => {
      const buttons = [...menu.querySelectorAll('button')];
      const at = buttons.indexOf(document.activeElement);
      if (key.key === 'ArrowDown' || key.key === 'ArrowUp') {
        key.preventDefault();
        buttons[(at + (key.key === 'ArrowDown' ? 1 : buttons.length - 1)) % buttons.length].focus();
      }
      if (key.key === 'Escape') { closeMenu(); opener?.focus?.(); }
      if (key.key === 'Tab') closeMenu();
    });
    document.body.append(menu);
    // At the pointer (or under the card, from the keyboard), kept inside the window.
    const fromKeyboard = event.clientX === 0 && event.clientY === 0;
    const box = card.getBoundingClientRect();
    const x = fromKeyboard ? box.left + 8 : event.clientX;
    const y = fromKeyboard ? box.top + 8 : event.clientY;
    menu.style.left = `${Math.max(4, Math.min(x, innerWidth - menu.offsetWidth - 4))}px`;
    menu.style.top = `${Math.max(4, Math.min(y, innerHeight - menu.offsetHeight - 4))}px`;
    menu.querySelector('button')?.focus();
  });

  document.addEventListener('pointerdown', event => { if (menu && !menu.contains(event.target)) closeMenu(); });
  window.addEventListener('scroll', closeMenu, { passive: true });
  window.addEventListener('resize', closeMenu);
  document.addEventListener('keydown', event => { if (event.key === 'Escape') closeMenu(); });
})();
