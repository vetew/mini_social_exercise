/*
 * Coding Assignment 2 - Design Claim 20 (Kiesler et al. 2011, ch. 4):
 * "Offering people reminders at the point of an action that may violate norms
 *  reduces the number of offenses."
 *
 * Live hint while typing. Every <textarea data-moderation-hint> gets a small box
 * under it. When the user stops typing for a moment, the text is sent to
 * /moderation/check (app.py), which runs the same rules as moderate_content().
 * If a rule would be broken, the box tells the user which one, before they press
 * "Post". Nothing is saved by this check.
 */
document.addEventListener('DOMContentLoaded', () => {
  const DELAY_MS = 600; // wait until the user pauses typing, so we do not call the server on every key

  document.querySelectorAll('textarea[data-moderation-hint]').forEach((textarea) => {
    // Create the hint box right after the text area (hidden until needed)
    const hint = document.createElement('div');
    hint.className = 'alert alert-warning small py-2 mt-2 mb-2 d-none';
    hint.setAttribute('role', 'status'); // screen readers announce changes politely
    textarea.insertAdjacentElement('afterend', hint);

    let timer = null;
    textarea.addEventListener('input', () => {
      clearTimeout(timer);
      timer = setTimeout(() => checkText(textarea.value, hint), DELAY_MS);
    });

    // Also check text that is already in the box (e.g. on the reminder page)
    if (textarea.value.trim()) {
      checkText(textarea.value, hint);
    }
  });

  async function checkText(text, hint) {
    if (!text.trim()) {
      hint.classList.add('d-none');
      return;
    }
    try {
      const response = await fetch('/moderation/check', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ content: text }),
      });
      if (!response.ok) return; // e.g. not logged in: just show nothing
      const result = await response.json();
      showHint(result, hint);
    } catch (err) {
      // The hint is only a helper; posting still works (and is checked) without it
      hint.classList.add('d-none');
    }
  }

  function showHint(result, hint) {
    if (!result.violations.length) {
      hint.classList.add('d-none');
      return;
    }
    // Build the text with textContent (not innerHTML) so user text can never inject HTML
    hint.textContent = '';
    const title = document.createElement('strong');
    title.textContent = result.removed
      ? 'Heads up: this text would be removed by our rules: '
      : 'Heads up: this text goes against our rules: ';
    hint.appendChild(title);

    result.violations.forEach((v, i) => {
      const link = document.createElement('a');
      link.href = '/rules#rule-' + v.rule.replaceAll('.', '-');
      link.target = '_blank';
      link.textContent = v.title + ' (Rule ' + v.rule + ')';
      if (i > 0) hint.appendChild(document.createTextNode(', '));
      hint.appendChild(link);
    });
    hint.appendChild(document.createTextNode('. You can still edit it before posting.'));
    hint.classList.remove('d-none');
  }
});
