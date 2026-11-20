const html = document.documentElement;
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
  } catch (error) {
    console.error('Error fetching theme preference:', error);
  }
  return 'system';
}

function getSystemTheme() {
  return window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
}

function setTheme(theme) {
  html.setAttribute('data-theme', theme);
}

function applyThemePreference(preference) {
  if (preference === 'system') {
    setTheme(getSystemTheme());
  } else {
    setTheme(preference);
  }
  savedThemePreference = preference;
  localStorage.setItem('litkeeper_theme', preference);
}

async function initTheme() {
  const preference = await fetchThemePreference();
  applyThemePreference(preference);
}

window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', (e) => {
  if (savedThemePreference === 'system') {
    setTheme(e.matches ? 'dark' : 'light');
  }
});

function showHiddenWarnings() {
  sessionStorage.removeItem('mountWarningDismissed');
  sessionStorage.removeItem('secretKeyWarningDismissed');
  sessionStorage.removeItem('missingMetadataBannerDismissed');
  sessionStorage.removeItem('syncBannerDismissed');

  const mountWarning = document.getElementById('mountWarning');
  if (mountWarning) {
    mountWarning.classList.remove('hidden');
  }

  const secretKeyWarning = document.getElementById('secretKeyWarning');
  if (secretKeyWarning) {
    secretKeyWarning.classList.remove('hidden');
  }

  if (typeof loadMissingMetadata === 'function') {
    loadMissingMetadata();
  }

  const syncBanner = document.getElementById('syncBanner');
  if (syncBanner) {
    syncBanner.classList.remove('hidden');
  }
}

// The header's light/dark button: flips the current look and remembers the choice.
async function toggleTheme() {
  const next = html.getAttribute('data-theme') === 'dark' ? 'light' : 'dark';
  applyThemePreference(next);
  try {
    await fetch('/settings/theme-preference', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ theme: next }),
    });
  } catch (error) {
    console.error('Error saving theme preference:', error);
  }
}

window.applyThemePreference = applyThemePreference;
window.toggleTheme = toggleTheme;
window.showHiddenWarnings = showHiddenWarnings;

initTheme();

window.addEventListener('pageshow', async (event) => {
  if (event.persisted || performance.getEntriesByType('navigation')[0]?.type === 'back_forward') {
    const preference = await fetchThemePreference();
    applyThemePreference(preference);
  }
});
