// Shared modal helpers
function showModal(id) { document.getElementById(id).classList.remove('hidden'); }
function closeModal(id) { document.getElementById(id).classList.add('hidden'); }

// Close modal on backdrop click
document.addEventListener('click', e => {
  if (e.target.classList.contains('modal')) {
    e.target.classList.add('hidden');
  }
});

// Close modal on Escape
document.addEventListener('keydown', e => {
  if (e.key === 'Escape') {
    document.querySelectorAll('.modal:not(.hidden)').forEach(m => m.classList.add('hidden'));
    closeUserMenu();
    closeMobileMenu();
  }
});

// User menu
function toggleUserMenu() {
  document.getElementById('user-menu')?.classList.toggle('hidden');
}

function closeUserMenu() {
  document.getElementById('user-menu')?.classList.add('hidden');
}

document.addEventListener('click', e => {
  if (!e.target.closest('.nav-user')) closeUserMenu();
});

// Mobile nav drawer
function toggleMobileMenu() {
  document.getElementById('mobile-drawer')?.classList.contains('open') ? closeMobileMenu() : openMobileMenu();
}

function openMobileMenu() {
  document.getElementById('mobile-drawer')?.classList.add('open');
  document.getElementById('mobile-drawer-overlay')?.classList.add('open');
  document.body.classList.add('drawer-open');
}

function closeMobileMenu() {
  document.getElementById('mobile-drawer')?.classList.remove('open');
  document.getElementById('mobile-drawer-overlay')?.classList.remove('open');
  document.body.classList.remove('drawer-open');
}

// Share a quotation: any logged-in teammate who opens the link lands
// straight on it (auth's `next` redirect handles anyone not yet signed in).
async function shareQuote(id) {
  if (!id) return;
  const url = `${window.location.origin}/quotation/${id}/edit`;
  try {
    await navigator.clipboard.writeText(url);
    showToast('Link copied — any Slate team member can open it');
  } catch (e) {
    try {
      prompt('Copy this link:', url);
    } catch (e2) {
      alert('Share link:\n' + url);
    }
  }
}

// Toast
function showToast(msg) {
  let t = document.getElementById('toast');
  if (!t) {
    t = document.createElement('div');
    t.id = 'toast';
    t.className = 'toast';
    document.body.appendChild(t);
  }
  t.textContent = msg;
  t.classList.add('show');
  setTimeout(() => t.classList.remove('show'), 2500);
}
