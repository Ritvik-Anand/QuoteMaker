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
