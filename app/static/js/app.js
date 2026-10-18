/* EZ Mark — UI helpers
   ─────────────────────────────────────────────────────────── */

/* Toast notifications */
function showToast(message, type = 'success') {
  const container = document.getElementById('toast-container');
  if (!container) return;

  const toast = document.createElement('div');
  toast.className = `toast ${type}`;
  toast.textContent = message;

  container.appendChild(toast);
  setTimeout(() => {
    toast.style.opacity = '0';
    toast.style.transform = 'translateY(-4px)';
    toast.style.transition = 'opacity .2s, transform .2s';
    setTimeout(() => toast.remove(), 200);
  }, 3000);
}

/* Confirmation dialogs for delete/archive actions */
document.addEventListener('DOMContentLoaded', () => {

  /* Auto-dismiss alerts with data-autodismiss */
  document.querySelectorAll('[data-autodismiss]').forEach(el => {
    const delay = parseInt(el.dataset.autodismiss, 10) || 4000;
    setTimeout(() => {
      el.style.transition = 'opacity .3s';
      el.style.opacity = '0';
      setTimeout(() => el.remove(), 300);
    }, delay);
  });

  /* Danger confirm: forms with data-confirm get a browser confirm() before submit */
  document.addEventListener('submit', (e) => {
    const form = e.target;
    const msg = form.dataset.confirm;
    if (msg && !confirm(msg)) {
      e.preventDefault();
    }
  });

  /* Simple tab switching: data-tab-group + data-tab + data-tab-panel */
  document.querySelectorAll('[data-tab-group]').forEach(group => {
    const tabs = group.querySelectorAll('[data-tab]');
    const panels = group.querySelectorAll('[data-tab-panel]');

    tabs.forEach(tab => {
      tab.addEventListener('click', () => {
        const target = tab.dataset.tab;
        tabs.forEach(t => t.classList.toggle('is-active', t.dataset.tab === target));
        panels.forEach(p => {
          p.hidden = p.dataset.tabPanel !== target;
        });
      });
    });
  });

  /* Active row highlight in review tables */
  document.querySelectorAll('.review-row-link').forEach(row => {
    row.addEventListener('click', () => {
      document.querySelectorAll('.review-row-link').forEach(r => r.classList.remove('is-active'));
      row.classList.add('is-active');
    });
  });

  /* Keyboard shortcuts for review page */
  const reviewPage = document.getElementById('review-workspace');
  if (reviewPage) {
    document.addEventListener('keydown', (e) => {
      if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA' || e.target.tagName === 'SELECT') return;
      if (e.ctrlKey || e.metaKey || e.altKey) return;

      /* 1–9: quick-submit answer */
      if (e.key >= '1' && e.key <= '9') {
        const btn = document.querySelector(`.review-choice-btn[data-answer="${e.key}"]`);
        if (btn) btn.closest('form')?.submit();
      }

      /* B: blank */
      if (e.key === 'b' || e.key === 'B') {
        const blankForm = document.getElementById('review-blank-form');
        if (blankForm) blankForm.submit();
      }

      /* X: invalid */
      if (e.key === 'x' || e.key === 'X') {
        const invalidForm = document.getElementById('review-invalid-form');
        if (invalidForm) invalidForm.submit();
      }

      /* N / J / Enter / ArrowDown: next item, K / ArrowUp: previous item */
      if (['n', 'N', 'j', 'J', 'Enter', 'ArrowDown'].includes(e.key)) {
        const nextLink = document.getElementById('review-next-link');
        if (nextLink) {
          e.preventDefault();
          nextLink.click();
        }
      }
      if (e.key === 'k' || e.key === 'K' || e.key === 'ArrowUp') {
        const prevLink = document.getElementById('review-prev-link');
        if (prevLink) {
          e.preventDefault();
          prevLink.click();
        }
      }
    });
  }

  /* Progressive disclosure: show/hide fields based on radio selection */
  document.querySelectorAll('[data-reveal-trigger]').forEach(radio => {
    radio.addEventListener('change', () => {
      const group = radio.closest('[data-reveal-group]');
      if (!group) return;
      const target = radio.dataset.revealTrigger;
      group.querySelectorAll('[data-reveal-for]').forEach(panel => {
        panel.hidden = panel.dataset.revealFor !== target;
      });
    });
  });

  /* Fire on load so pre-selected radios hide/show correctly */
  document.querySelectorAll('[data-reveal-trigger]:checked').forEach(radio => {
    radio.dispatchEvent(new Event('change'));
  });

  /* Bulk selection footer for list pages (テスト管理 / テンプレート).
     Card checkboxes live inside each card; the sticky footer stays hidden until at
     least one card is selected. */
  const bulkFooter = document.getElementById('bulk-footer');
  if (bulkFooter) {
    const checkboxes = Array.from(document.querySelectorAll('input[data-card-checkbox]'));
    const countEl = bulkFooter.querySelector('[data-bulk-count-text]');
    const countTemplate = countEl ? countEl.dataset.bulkCountTemplate || '{count}' : '{count}';

    const update = () => {
      let selected = 0;
      checkboxes.forEach(cb => {
        const card = cb.closest('.summary-card');
        if (card) card.classList.toggle('is-selected', cb.checked);
        if (cb.checked) selected += 1;
      });
      bulkFooter.hidden = selected === 0;
      document.body.classList.toggle('has-bulk-footer', selected > 0);
      if (countEl) countEl.textContent = countTemplate.replace('{count}', selected);
    };

    checkboxes.forEach(cb => cb.addEventListener('change', update));

    const cancelBtn = bulkFooter.querySelector('[data-bulk-cancel]');
    if (cancelBtn) {
      cancelBtn.addEventListener('click', () => {
        checkboxes.forEach(cb => { cb.checked = false; });
        update();
      });
    }

    update();
  }

});

/* HTMX event: show toast on hx-swap success with data-toast-success attribute */
document.body.addEventListener('htmx:afterSwap', (event) => {
  const toast = event.detail.target.dataset.toastSuccess;
  if (toast) showToast(toast);
});
