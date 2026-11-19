// The Collect page: after a page was taken, count down and close the tab the bookmark opened, so you're back on
// the job site for the next page of results. Stays open on a problem, on a question, or when you say so.
(function () {
  const box = document.getElementById('collect-box');
  const closing = document.getElementById('collect-closing');
  if (!box || !box.dataset.close || !closing) return;
  const leftEl = document.getElementById('collect-left');
  let left = 4;
  closing.hidden = false;
  const timer = setInterval(() => {
    left -= 1;
    leftEl.textContent = String(Math.max(left, 0));
    if (left > 0) return;
    clearInterval(timer);
    window.close();
    // A browser that won't let a page close its own tab leaves it open: say so instead of a stuck countdown.
    setTimeout(() => { closing.textContent = 'You can close this tab.'; }, 400);
  }, 1000);
  document.getElementById('collect-keep').addEventListener('click', (e) => {
    e.preventDefault();
    clearInterval(timer);
    closing.hidden = true;
  });
})();
