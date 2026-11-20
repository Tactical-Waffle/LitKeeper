const metadataModal = new MetadataModal();

function initializeLibraryFilters() {
  const sortBy = document.getElementById('sortBy');
  const categoryFilter = document.getElementById('categoryFilter');
  const sourceFilter = document.getElementById('sourceFilter');
  const sortOrderToggle = document.getElementById('sortOrderToggle');

  const savedSortBy = localStorage.getItem('library_sort_by') || 'date';
  const savedCategory = localStorage.getItem('library_category') || 'all';
  const savedSource = localStorage.getItem('library_source') || 'all';
  const savedSortOrder = localStorage.getItem('library_sort_order') || 'desc';
  const savedQueueOnly = localStorage.getItem('library_queue_only') === 'true';

  applyQueueViewUI(savedQueueOnly);

  if (sortBy) {
    sortBy.value = savedSortBy;
  }
  if (categoryFilter) {
    categoryFilter.value = savedCategory;
  }
  if (sourceFilter) {
    sourceFilter.value = savedSource;
  }
  if (sortOrderToggle) {
    sortOrderToggle.value = savedSortOrder;
    updateSortOrderIcon(sortOrderToggle, savedSortOrder);
  }

  const hasSavedPreferences = localStorage.getItem('library_sort_by') ||
                               localStorage.getItem('library_category') ||
                               localStorage.getItem('library_source') ||
                               localStorage.getItem('library_sort_order') ||
                               savedQueueOnly;
  
  if (hasSavedPreferences) {
    document.body.addEventListener('htmx:afterSwap', function onSwap(e) {
      if (e.detail.target.id === 'library-content') {
        e.detail.target.style.visibility = '';
        document.body.removeEventListener('htmx:afterSwap', onSwap);
      }
    });
    triggerLibraryFilter();
  }
}

function updateSortOrderIcon(button, order) {
  const svg = button.querySelector('svg path');
  const label = button.querySelector('#sortOrderLabel');
  if (order === 'asc') {
    svg.setAttribute('d', 'M5 15l7-7 7 7');
    button.setAttribute('title', 'Ascending order');
    if (label) label.textContent = 'Ascending';
  } else {
    svg.setAttribute('d', 'M19 9l-7 7-7-7');
    button.setAttribute('title', 'Descending order');
    if (label) label.textContent = 'Descending';
  }
}

// --- Reading queue view ---

function isQueueViewActive() {
  const input = document.getElementById('queueOnlyInput');
  return !!input && input.value === 'true';
}

function applyQueueViewUI(queueOnly) {
  const input = document.getElementById('queueOnlyInput');
  if (input) input.value = queueOnly ? 'true' : 'false';
  const allTab = document.getElementById('libraryViewAllTab');
  const queueTab = document.getElementById('libraryViewQueueTab');
  if (allTab) allTab.setAttribute('aria-selected', queueOnly ? 'false' : 'true');
  if (queueTab) queueTab.setAttribute('aria-selected', queueOnly ? 'true' : 'false');
}

// Switching into the queue view orders it "up next" first (oldest queued at
// the top); switching back restores whatever sort the library had before.
window.setLibraryQueueView = function(queueOnly) {
  if (isQueueViewActive() === queueOnly) return;

  const sortBySelect = document.getElementById('sortBy');
  const orderToggle = document.getElementById('sortOrderToggle');
  let nextSortBy, nextSortOrder;

  if (queueOnly) {
    if (sortBySelect && orderToggle) {
      localStorage.setItem('library_sort_before_queue', JSON.stringify({
        sort_by: sortBySelect.value,
        sort_order: orderToggle.value,
      }));
    }
    nextSortBy = 'queued';
    nextSortOrder = 'asc';
  } else {
    let prev = null;
    try { prev = JSON.parse(localStorage.getItem('library_sort_before_queue') || 'null'); } catch (_) {}
    localStorage.removeItem('library_sort_before_queue');
    nextSortBy = (prev && prev.sort_by && prev.sort_by !== 'queued') ? prev.sort_by : 'date';
    nextSortOrder = (prev && prev.sort_order) || 'desc';
  }

  if (sortBySelect) {
    sortBySelect.value = nextSortBy;
    localStorage.setItem('library_sort_by', nextSortBy);
  }
  if (orderToggle) {
    orderToggle.value = nextSortOrder;
    localStorage.setItem('library_sort_order', nextSortOrder);
    updateSortOrderIcon(orderToggle, nextSortOrder);
  }

  localStorage.setItem('library_queue_only', queueOnly ? 'true' : 'false');
  applyQueueViewUI(queueOnly);
  refreshLibrary();
};

function triggerLibraryFilter() {
  const sortBy = document.getElementById('sortBy');
  if (sortBy) {
    htmx.trigger(sortBy, 'change');
  }
}

const sortOrderToggle = document.getElementById('sortOrderToggle');
if (sortOrderToggle) {
  let pendingOrder = null;
  
  sortOrderToggle.addEventListener('click', function(e) {
    const currentOrder = this.value;
    const newOrder = currentOrder === 'desc' ? 'asc' : 'desc';
    pendingOrder = newOrder;
    this.value = newOrder;
    localStorage.setItem('library_sort_order', newOrder);
    updateSortOrderIcon(this, newOrder);
  });
  
  sortOrderToggle.addEventListener('htmx:configRequest', function(e) {
    if (pendingOrder) {
      e.detail.parameters.sort_order = pendingOrder;
      pendingOrder = null;
    }
  });
}

const sortBy = document.getElementById('sortBy');
if (sortBy) {
  sortBy.addEventListener('change', function() {
    localStorage.setItem('library_sort_by', this.value);
  });
}

const categoryFilter = document.getElementById('categoryFilter');
if (categoryFilter) {
  categoryFilter.addEventListener('change', function() {
    localStorage.setItem('library_category', this.value);
  });
}

const sourceFilter = document.getElementById('sourceFilter');
if (sourceFilter) {
  sourceFilter.addEventListener('change', function() {
    localStorage.setItem('library_source', this.value);
  });
}

document.addEventListener('DOMContentLoaded', () => {
  initializeLibraryFilters();
});

window.addEventListener('pageshow', (e) => {
  if (e.persisted && sessionStorage.getItem('litkeeper_reload')) {
    sessionStorage.removeItem('litkeeper_reload');
    window.location.reload();
  }
});
