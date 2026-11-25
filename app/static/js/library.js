const metadataModal = new MetadataModal();

// The filters in the Filters menu stay as they were left (across visits) until Clear Filters.
// Sort, source, categories and the queue view are kept under their own keys below.
const STICKY_FILTERS = { userRatingFilter: 'user_rating', minScoreFilter: 'min_community_score',
                         readStatusFilter: 'read_status', pageLengthFilter: 'page_length' };
const PAGE_LENGTHS = { short: ['', '9'], medium: ['10', '50'], long: ['51', ''] };

function savedLibraryFilters() {
  try {
    const saved = JSON.parse(localStorage.getItem('library_filters') || '{}');
    return saved && typeof saved === 'object' && !Array.isArray(saved) ? saved : {};
  } catch (_) { return {}; }
}

function setPageLengthRange(select) {
  const [min, max] = PAGE_LENGTHS[select.value] || ['', ''];
  select.dataset.minPages = min;
  select.dataset.maxPages = max;
}

function saveLibraryFilters() {
  const saved = {};
  for (const [id, key] of Object.entries(STICKY_FILTERS)) {
    const el = document.getElementById(id);
    if (el && el.value) saved[key] = el.value;
  }
  try {
    if (Object.keys(saved).length) localStorage.setItem('library_filters', JSON.stringify(saved));
    else localStorage.removeItem('library_filters');
  } catch (_) {}
}

// Put the saved filters back in their menus; returns whether any was applied.
function applySavedLibraryFilters() {
  const saved = savedLibraryFilters();
  let applied = false;
  for (const [id, key] of Object.entries(STICKY_FILTERS)) {
    const el = document.getElementById(id);
    if (!el || !saved[key]) continue;
    if (![...el.options].some(o => o.value === saved[key])) continue;  // An option that no longer exists.
    el.value = saved[key];
    if (id === 'pageLengthFilter') setPageLengthRange(el);
    applied = true;
  }
  return applied;
}

Object.keys(STICKY_FILTERS).forEach(id => document.getElementById(id)?.addEventListener('change', saveLibraryFilters));

// Searching, sorting and changing the source (htmx requests) keep the page length too: its menu sends a name
// ("long"), but the library is filtered by pages.
document.body.addEventListener('htmx:configRequest', event => {
  if (!String(event.detail.path || '').startsWith('/library/filter')) return;
  const length = document.getElementById('pageLengthFilter');
  if (!length) return;
  if (length.dataset.minPages) event.detail.parameters.min_pages = length.dataset.minPages;
  if (length.dataset.maxPages) event.detail.parameters.max_pages = length.dataset.maxPages;
});

function initializeLibraryFilters() {
  const sortBy = document.getElementById('sortBy');
  const sourceFilter = document.getElementById('sourceFilter');
  const sortOrderToggle = document.getElementById('sortOrderToggle');

  const savedSortBy = localStorage.getItem('library_sort_by') || 'date';
  // Saved categories: a JSON list now; earlier builds saved one name (or "all").
  let savedCategories = [];
  try {
    const raw = localStorage.getItem('library_category');
    savedCategories = raw && raw.startsWith('[') ? JSON.parse(raw) : (raw && raw !== 'all' ? [raw] : []);
    if (!savedCategories.length && raw !== null) localStorage.removeItem('library_category');
  } catch (_) {}
  const savedSource = localStorage.getItem('library_source') || 'all';
  const savedSortOrder = localStorage.getItem('library_sort_order') || 'desc';
  const savedQueueOnly = localStorage.getItem('library_queue_only') === 'true';
  const savedFilters = applySavedLibraryFilters();
  if (!savedCategories.length && !localStorage.getItem('library_sort_by') && !localStorage.getItem('library_source') &&
      !localStorage.getItem('library_sort_order') && !savedQueueOnly && !savedFilters) {
    const content = document.getElementById('library-content');
    if (content) content.style.visibility = '';  // Nothing to apply: show the first page as it came.
  }

  applyQueueViewUI(savedQueueOnly);

  if (sortBy) {
    sortBy.value = savedSortBy;
  }
  document.querySelectorAll('.category-filter-box').forEach(box => { box.checked = savedCategories.includes(box.value); });
  if (typeof updateFiltersActiveDot === 'function') updateFiltersActiveDot();
  if (sourceFilter) {
    sourceFilter.value = savedSource;
  }
  if (sortOrderToggle) {
    sortOrderToggle.value = savedSortOrder;
    updateSortOrderIcon(sortOrderToggle, savedSortOrder);
  }

  const hasSavedPreferences = localStorage.getItem('library_sort_by') ||
                               savedCategories.length ||
                               localStorage.getItem('library_source') ||
                               localStorage.getItem('library_sort_order') ||
                               savedQueueOnly || savedFilters;
  
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
