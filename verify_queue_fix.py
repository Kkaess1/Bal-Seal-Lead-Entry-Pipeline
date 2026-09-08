from playwright.sync_api import sync_playwright

html = '''<!doctype html><html><body>
<nav><a href="/queue">Input Queue <span>5</span></a></nav>
<main>
  <h2>Input Queue — Session Review (5)</h2>
  <form action="/queue/approve_all"><button>Confirm &amp; Send 5 to Oracle</button></form>
  <div class="card" id="queue-card-1">A</div>
  <div class="card" id="queue-card-2">B</div>
  <div class="card" id="queue-card-3">C</div>
  <div class="card" id="queue-card-4">D</div>
  <div class="card" id="queue-card-5">E</div>
</main>
<script>
function refreshQueueSummary() {
  var reviewCards = document.querySelectorAll('[id^="queue-card-"]');
  var remaining = reviewCards.length;
  var heading = document.querySelector('h2');
  if (heading && heading.textContent.indexOf('Input Queue') !== -1) {
    heading.textContent = 'Input Queue — Session Review (' + remaining + ')';
  }

  var queueForm = document.querySelector('form[action$="/queue/approve_all"]');
  if (queueForm) {
    var confirmBtn = queueForm.querySelector('button');
    if (confirmBtn) {
      if (remaining === 0) {
        queueForm.remove();
      } else {
        var reviewedLabel = remaining === 1 ? 'lead' : 'leads';
        confirmBtn.textContent = 'Confirm & Send ' + remaining + ' to Oracle';
        confirmBtn.setAttribute('onclick', "return confirm('Send " + remaining + " reviewed " + reviewedLabel + " to Oracle?');");
      }
    }
  }

  var queueNav = document.querySelector('nav a[href="/queue"]');
  if (queueNav) {
    var badge = queueNav.querySelector('span');
    var total = document.querySelectorAll('.card').length;
    if (total > 0) {
      if (!badge) {
        badge = document.createElement('span');
        badge.style.background = 'var(--accent)';
        badge.style.color = '#fff';
        badge.style.borderRadius = '10px';
        badge.style.padding = '2px 7px';
        badge.style.fontSize = '0.75rem';
        badge.style.marginLeft = '2px';
        queueNav.appendChild(badge);
      }
      badge.textContent = total;
    } else if (badge) {
      badge.remove();
    }
  }
}

function removeLeadCards(cards, nextCard) {
  cards = cards.filter(Boolean);
  cards.forEach(function(card) { card.remove(); });
  refreshQueueSummary();
}
</script>
</body></html>'''

with sync_playwright() as pw:
    browser = pw.chromium.launch(headless=True)
    page = browser.new_page()
    page.set_content(html)
    page.evaluate("removeLeadCards([document.getElementById('queue-card-1')], null)")
    page.wait_for_timeout(200)
    print('heading=' + page.locator('h2').inner_text())
    print('button=' + page.locator('form[action$="/queue/approve_all"] button').inner_text())
    print('badge=' + page.locator('nav a[href="/queue"] span').inner_text())
    print('count=' + str(page.locator('[id^="queue-card-"]').count()))
    browser.close()
