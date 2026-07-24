import logging
import time
from pathlib import Path
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

from dotenv import load_dotenv
load_dotenv()

import secrets_store
from phone_parser import parse_phone

logger = logging.getLogger(__name__)

SHORT = 10_000   # ms — quick interactions
MEDIUM = 30_000  # ms — page loads
LONG   = 60_000  # ms — Oracle can be slow after login


class OracleLeadAutomator:

    def __init__(self, config, username, password, log_dir='logs'):
        self.base_url    = config['base_url'].rstrip('/')
        self.lead_source = config.get('lead_source', 'Trade Show')
        self.headless    = config.get('headless', False)
        self.slow_mo     = config.get('slow_mo_ms', 150)
        self.manual_login = config.get('manual_login', True)
        self.username    = username or secrets_store.get_secret('ORACLE_USERNAME')
        self.password    = password or secrets_store.get_secret('ORACLE_PASSWORD')
        self.log_dir     = Path(log_dir)
        self.log_dir.mkdir(exist_ok=True)

        # Session persistence -- used by pipeline_submit.py for unattended runs.
        # main.py's interactive flow doesn't set these, so storage_state_path is
        # None there and __enter__ just launches a fresh, empty browser context
        # exactly as it always has.
        storage_state_path = config.get('storage_state_path')
        self.storage_state_path = Path(storage_state_path) if storage_state_path else None
        self.session_check_timeout = config.get('session_check_timeout_ms', 10_000)

        self._pw = self._browser = self._ctx = self.page = None

    # ------------------------------------------------------------------
    # Context manager
    # ------------------------------------------------------------------

    def __enter__(self):
        self._pw      = sync_playwright().start()
        self._browser = self._pw.chromium.launch(
            headless=self.headless,
            slow_mo=self.slow_mo,
        )
        context_kwargs = {'viewport': {'width': 1600, 'height': 900}}
        if self.storage_state_path and self.storage_state_path.exists():
            context_kwargs['storage_state'] = str(self.storage_state_path)
            logger.info(f'Restored browser session from {self.storage_state_path}')
        self._ctx = self._browser.new_context(**context_kwargs)
        self.page = self._ctx.new_page()
        return self

    def __exit__(self, *_):
        try:
            if self._browser:
                self._browser.close()
        finally:
            if self._pw:
                self._pw.stop()

    # ------------------------------------------------------------------
    # Session persistence (unattended runs)
    # ------------------------------------------------------------------

    def save_session(self):
        """Persist the current browser context's cookies/localStorage to
        storage_state_path, so a future run can restore this login without an
        interactive sign-in. Called by oracle_login_capture.py after a human
        logs in manually."""
        if not self.storage_state_path:
            raise RuntimeError('storage_state_path not configured')
        self.storage_state_path.parent.mkdir(parents=True, exist_ok=True)
        self._ctx.storage_state(path=str(self.storage_state_path))
        logger.info(f'Session saved to {self.storage_state_path}')

    def is_session_valid(self):
        """
        Navigate to Oracle and all the way through to the Leads list -- same
        bar _manual_login() holds a human to (able to see "Create Lead"), not
        just "did the landing page load". A restored session that's expired
        usually redirects to Oracle's login page partway through this, which
        surfaces here as a timeout on one of the navigation steps.

        Used by pipeline_submit.py to decide whether to proceed or abort --
        an expired/missing session should never silently fall back to
        interactive or password login during an unattended run.
        """
        try:
            self.page.goto(self.base_url, wait_until='domcontentloaded', timeout=LONG)
            self._auto_navigate_to_leads()
            return True
        except PWTimeout:
            return False

    def ensure_logged_in(self):
        """
        For fully unattended runs (pipeline_submit.py): try the restored
        storage_state session first (fast, no real sign-in traffic against
        Oracle). Only if that's actually invalid does this fall back to a
        real username/password auto-login -- this tenant has no MFA
        (confirmed by hand via oracle_login_capture.py previously), so a
        password-only login is sufficient here.

        Returns True once the bot is on a working Leads page one way or the
        other, False if login itself failed (e.g. bad/rotated credentials).
        Deliberately makes exactly one login attempt per call -- never
        retries on failure, to avoid repeated failed attempts against a
        production account (lockout risk).
        """
        if self.is_session_valid():
            return True
        logger.info('Session invalid or expired -- attempting fresh auto-login…')
        try:
            self._auto_login()
            self._auto_navigate_to_leads()
        except PWTimeout as e:
            logger.error(f'Auto-login failed: {e}')
            return False
        if self.storage_state_path:
            self.save_session()
        return True

    # ------------------------------------------------------------------
    # Login + navigate to Leads
    # ------------------------------------------------------------------

    def login(self):
        if self.manual_login:
            self._manual_login()
        else:
            self._auto_login()

    def navigate_to_leads(self):
        if self.manual_login:
            return  # User already navigated there manually
        self._auto_navigate_to_leads()

    def _manual_login(self):
        """Open the browser, let the user log in and navigate to Leads, then continue."""
        logger.info('Opening browser for manual login…')
        self.page.goto(self.base_url, wait_until='domcontentloaded', timeout=LONG)

        print()
        print('=' * 60)
        print('  A browser window has opened.')
        print()
        print('  1. Log in to Oracle Sales Cloud')
        print('  2. Click the Sales tab')
        print('  3. Click the Leads app')
        print('  4. Make sure you can see the "Create Lead" button')
        print()
        print('  Then come back here and press Enter to start.')
        print('=' * 60)
        input()

        # Verify the user is actually on the Leads page
        try:
            self.page.wait_for_selector('button:has-text("Create Lead")', timeout=10_000)
            logger.info('Confirmed: on Leads list page.')
        except PWTimeout:
            print()
            print('WARNING: Could not find the "Create Lead" button.')
            print('Make sure you are on the Leads list page, then press Enter again.')
            input()
            self.page.wait_for_selector('button:has-text("Create Lead")', timeout=15_000)

    def _auto_login(self):
        """
        Confirmed live against this tenant's actual sign-in page (an Oracle
        IDCS "Cloud Sign In" screen) -- it's a single-step form: username and
        password fields are BOTH present and visible from page load, one
        "Next" button submits both together. Deliberately fills both fields
        before clicking anything, rather than the more common two-step
        username-then-password IDCS flow -- clicking Next with only the
        username filled would submit with an empty password, which is a real
        failed-login attempt against a production account (lockout risk), not
        a harmless intermediate step.
        """
        logger.info('Navigating to Oracle Cloud sign-in…')
        self.page.goto(self.base_url, wait_until='domcontentloaded', timeout=LONG)
        username_sel = 'input[id*="username"]'
        password_sel = 'input[type="password"]'
        self.page.wait_for_selector(username_sel, timeout=LONG)
        self.page.wait_for_selector(password_sel, timeout=SHORT)
        self._fill_first_visible(username_sel, self.username)
        self._fill_first_visible(password_sel, self.password)
        self._click_first_visible(
            'button:has-text("Next"), button:has-text("Sign In"), input[value="Sign In"]'
        )
        # Confirmed live: this tenant lands on fscmUI/faces/AtkHomePageWelcome
        # after sign-in, not a crmUI path -- don't hardcode a specific
        # post-login module path, just confirm we've left the IDCS sign-in
        # domain. Actual success (reaching the Leads list) is verified
        # separately by _auto_navigate_to_leads(), called right after this by
        # ensure_logged_in() -- that's the real bar, this is just "did the
        # credentials get accepted at all".
        self.page.wait_for_url(lambda url: 'idcs-' not in url, timeout=LONG)
        self.page.wait_for_load_state('domcontentloaded', timeout=LONG)
        logger.info('Login successful.')

    def _auto_navigate_to_leads(self):
        """
        Real UI (confirmed against the live tenant) has no visible "Sales"
        tab at all -- navigation is via the hamburger Navigator icon, which
        opens an accordion-style flyout menu (Workspace / Redwood Sales /
        Sales / Customer Data Management / Tools / Configuration / Others),
        with "Leads" nested inside the "Sales" section.

        Confirmed live from a genuinely fresh login (not a restored session):
        every section starts collapsed, so "Sales" has to be expanded with
        its own click before "Leads" becomes visible/clickable -- it isn't
        directly visible on open the way it appeared to be when this was
        first tuned against an already-logged-in session that happened to
        have it pre-expanded. Only clicks the expand step if Leads isn't
        already visible, so this still works from either starting state.
        """
        logger.info('Navigating to Leads…')
        self._click_first_visible(
            'a[title="Navigator"], button[title="Navigator"], [aria-label="Navigator"]'
        )
        self.page.wait_for_timeout(1000)
        leads_link = self.page.locator(
            'a:text-is("Leads"), [title="Leads"], span:text-is("Leads")'
        ).first
        if not leads_link.is_visible():
            self._js_click_first_visible('text="Sales"')
            self.page.wait_for_timeout(800)
        leads_link.click()
        self.page.wait_for_load_state('domcontentloaded', timeout=MEDIUM)
        self.page.wait_for_selector('button:has-text("Create Lead")', timeout=MEDIUM)
        logger.info('On Leads list page.')

    # ------------------------------------------------------------------
    # Create a single lead
    # ------------------------------------------------------------------

    def create_lead(self, lead, trade_show_name, source_label='Tradeshow'):
        """
        Open the Create Lead form and fill every field.
        Does NOT submit — the user reviews and clicks Save and Close manually.
        Returns the project name string.

        `source_label` defaults to 'Tradeshow' for main.py's existing Excel-import
        flow. pipeline_submit.py passes something more accurate for email-sourced
        leads (e.g. 'Inquiry') -- these aren't trade show contacts, and mislabeling
        them would be a real, visible inaccuracy in Oracle, not just cosmetic.
        """
        # Build the Project Name
        org   = (lead.get('organization') or '').upper().strip()
        first = (lead.get('first_name') or '').strip()
        last  = (lead.get('last_name')  or '').strip()
        name  = f'{first} {last}'.strip()
        project_name = f'{org} - {name} - {source_label} - {trade_show_name}'

        logger.info(f'Filling: {project_name}')

        # Open the form
        create_btn_sel = 'button:has-text("Create Lead"), a:has-text("Create Lead"), [title*="Create Lead"], [aria-label*="Create Lead"]'
        self.page.locator(create_btn_sel).first.click(force=True)
        try:
            self.page.wait_for_selector(
                'button:has-text("Save and Close")',
                state='visible',
                timeout=MEDIUM,
            )
        except PWTimeout:
            logger.warning('  "Save and Close" did not appear after the first click -- retrying')
            self.page.locator(create_btn_sel).first.click(force=True)
            self.page.wait_for_selector(
                'button:has-text("Save and Close")',
                state='visible',
                timeout=LONG,
            )
        # Brief pause for ADF JS to finish rendering all visible fields
        self.page.wait_for_timeout(2000)

        # --- Left column ---
        self._fill_by_label('Project Name', project_name)

        # Existing Account/Contact matching is disabled for now (2026-07-20) --
        # selecting a match via _search_and_select_existing() leaves Oracle's
        # "accountExtnPickerPopupId" widget in a flaky state that can pop open
        # a full "Select: Existing Account" modal on a later, unrelated click
        # and block every remaining field on the form. Confirmed this is a
        # genuine race condition (not a simple missing-dismiss-step), not yet
        # reliably fixed. Keaton chose to always create a New Account/New
        # Contact for now (accepting possible duplicate records in Oracle)
        # rather than block real submissions on this bug. _search_and_select_existing()
        # itself is untouched/still used for Owner -- only this Account/Contact
        # call site is bypassed.
        self._fill_by_label('New Account', lead.get('organization'))
        self._fill_by_label('New Contact', name if name else None)

        self._fill_by_label('Job Title',    lead.get('title'))

        # Phone — area code in small box, number (no hyphens) in large box
        self._fill_phone(lead.get('phone'))

        # --- Right column ---
        self._fill_by_label('Contact Email', lead.get('email'))
        self._fill_by_label('Lead Details',  lead.get('lead_details'))

        # Lead Source dropdown
        self._select_dropdown('Lead Source', self.lead_source)

        # Rank dropdown -- 'Cold' is deliberately never selected here, even if
        # somehow present on the lead: a lead that's actually cold/spam should
        # never have reached submission in the first place (see
        # lead_extractor.py's rank criteria), so treat it as unset rather than
        # send a value Oracle shouldn't be seeing at this stage at all.
        # Casing is normalized here too (not just in lead_extractor.py) since a
        # human can freely retype this field in the dashboard before approving.
        rank = (lead.get('rank') or '').strip().capitalize()
        if rank in ('Hot', 'Warm'):
            self._select_dropdown('Rank', rank)
        elif rank == 'Cold':
            logger.warning(f'  Rank was "Cold" for {project_name} -- leaving Rank blank')

        # Market dropdown -- mandatory field in Oracle. Default to INDUSTRIAL if unassigned.
        market = (lead.get('market') or 'INDUSTRIAL').strip().upper()
        self._select_dropdown('Market', market)

        # Owner (Territory Manager) -- only overridden when territory_map.assign_tm()
        # produced a confident match; otherwise Oracle's default owner (whoever is
        # logged in) stays, and the lead should be reassigned manually. See
        # territory_map.py for what is/isn't covered yet.
        #
        # Owner is a person-search field (the same AFAutoSuggestItem live-search
        # widget as Existing Account/Contact), not a static click-to-reveal
        # dropdown -- confirmed live after a real submission (Interlune) silently
        # kept the default owner instead of the correctly-assigned Brian Carter,
        # because _select_dropdown() only clicks and waits for a pre-populated
        # option list that this field never shows without being typed into first.
        assigned_tm = lead.get('assigned_tm')
        if assigned_tm and not self._search_and_select_existing('Owner', assigned_tm):
            logger.warning(f'  Could not match Owner "{assigned_tm}" -- default owner left in place')

        # --- Address ---
        country = lead.get('country') or ''
        is_us = country.upper() in ('USA', 'US', 'UNITED STATES', 'U.S.A.', '')
        if not is_us and country:
            self._select_dropdown('Country', country)

        self._fill_by_label('Postal Code',    lead.get('zip'))
        self._fill_by_label('Address Line 1', lead.get('address1'))
        self._fill_by_label('Address Line 2', lead.get('address2'))
        self._fill_by_label('City',           lead.get('city'))
        # State skipped — Oracle dropdown does not accept abbreviations like TX

        # --- Email Attachment ---
        email_file = lead.get('email_file') or lead.get('raw_email_path')
        if not email_file and lead.get('id'):
            import db
            email_file = db.get_lead_email_attachment(lead['id'])
        if email_file:
            self._attach_file(email_file)

        # Form is filled — user will review and save manually
        return project_name

    def _attach_file(self, file_path):
        """
        Attaches a file to the lead in Oracle Sales Cloud.
        Clicks Oracle's "Add Attachment" / "Attach File" button and uploads the email file.
        """
        if not file_path or not Path(file_path).exists():
            return False
        path_obj = Path(file_path).resolve()
        logger.info(f'  Attempting to attach email file: {path_obj}')

        # Selectors for Oracle ADF & Redwood attachment widgets
        file_input_sel = 'input[type="file"], [id*="attachment"] input, [id*="Attach"] input'
        attach_btn_sel = (
            'button:has-text("Add Attachment"), a:has-text("Add Attachment"), '
            'button:has-text("Attach File"), a:has-text("Attach File"), '
            'button:has-text("Attachment"), [aria-label*="Attachment"], [title*="Attachment"]'
        )

        try:
            # 1. Try file chooser by clicking the Attach button
            attach_btn = self.page.locator(attach_btn_sel).first
            if attach_btn.is_visible(timeout=4_000):
                logger.info('  Found Oracle Attachment button -- clicking to attach file.')
                with self.page.expect_file_chooser(timeout=6_000) as fc_info:
                    attach_btn.click()
                file_chooser = fc_info.value
                file_chooser.set_files(str(path_obj))
                logger.info('  ✓ Successfully attached email file via file chooser.')
                return True

            # 2. Try direct file input element
            file_input = self.page.locator(file_input_sel).first
            if file_input.is_visible(timeout=3_000):
                file_input.set_input_files(str(path_obj))
                logger.info('  ✓ Successfully attached email file via file input element.')
                return True
        except Exception as e:
            logger.warning(f'  Attachment attempt skipped or non-blocking: {e}')
        return False

    def submit_lead(self, lead, trade_show_name, source_label='Tradeshow'):
        """
        Fill and fully submit a lead: create_lead() + click "Save and Close" +
        verify the result. Unlike create_lead() alone (used by main.py's
        human-supervised flow, which deliberately pauses here for a person to
        review), this does not pause -- it's for pipeline_submit.py's
        unattended runs, only ever called on leads a human already approved
        in the dashboard.

        Returns (success: bool, project_name: str, error_detail: str|None).
        On failure, a screenshot is saved via self.screenshot() and nothing
        is retried or force-navigated past -- the lead is left for the next
        attempt or manual inspection.
        """
        project_name = self.create_lead(lead, trade_show_name, source_label=source_label)

        # Dismiss any lingering dropdown popup / glass pane if present
        try:
            self.page.keyboard.press("Escape")
            self.page.wait_for_timeout(300)
        except Exception:
            pass

        save_btn = self.page.locator('button:has-text("Save and Close")').first
        save_btn.click(force=True)
        self.page.wait_for_timeout(2000)  # let Oracle's ADF validation run before checking

        error_text = self._get_popup_error()
        if error_text:
            logger.error(f'  Save failed for {project_name}: {error_text}')
            self.screenshot(f'submit_fail_{int(time.time())}.png')
            self._dismiss_popup()
            return False, project_name, error_text

        try:
            self.page.wait_for_selector(
                'button:has-text("Create Lead")',
                state='visible',
                timeout=MEDIUM,
            )
            logger.info(f'  Submitted: {project_name}')
            return True, project_name, None
        except PWTimeout:
            error_detail = ('Save and Close did not return to the Leads list within the '
                             'timeout (no error popup detected either)')
            logger.error(f'  {error_detail}: {project_name}')
            self.screenshot(f'submit_timeout_{int(time.time())}.png')
            return False, project_name, error_detail

    # ------------------------------------------------------------------
    # Helpers — field filling
    # ------------------------------------------------------------------

    def _fill_by_label(self, label_text, value):
        """
        Fill a text input or textarea identified by its visible label.
        Tries three strategies to handle Oracle ADF's non-standard ID patterns.
        """
        if not value or not str(value).strip():
            return
        value = str(value).strip()

        # Strategy 1: Playwright's built-in get_by_label (works for standard HTML)
        try:
            locator = self.page.get_by_label(label_text, exact=False).first
            locator.wait_for(state='visible', timeout=5_000)
            locator.click()
            locator.fill(value)
            return
        except Exception:
            pass

        # Strategy 2: Oracle ADF uses label[for="X::label"] → input id="X::content"
        try:
            label_el = self.page.locator(f'label:has-text("{label_text}")').first
            for_attr = label_el.get_attribute('for')  # e.g. "pt1:r1:0:f1::label"
            if for_attr:
                base_id = for_attr.replace('::label', '')
                field = self.page.locator(
                    f'#{base_id}\\:\\:content, #{base_id}'
                ).first
                field.wait_for(state='visible', timeout=5_000)
                field.click()
                field.fill(value)
                return
        except Exception:
            pass

        # Strategy 3: Find any input/textarea in the same table row as the label
        try:
            field = self.page.locator(
                f'label:has-text("{label_text}") >> xpath=ancestor::tr[1] >> '
                f'css=input[type="text"], textarea'
            ).first
            field.wait_for(state='visible', timeout=5_000)
            field.click()
            field.fill(value)
            return
        except Exception:
            pass

        logger.warning(f"  Could not fill '{label_text}' — all strategies failed")

    def _fill_phone(self, raw_phone):
        """Parse the phone string and fill the correct Oracle phone fields."""
        parsed = parse_phone(raw_phone)
        if not parsed:
            return

        if parsed['type'] == 'work':
            # Contact Work Phone: [country_code selector][area code input][number input]
            # The country code is already set to 1 by default; only change if different.
            if parsed['country_code'] != '1':
                try:
                    cc_sel = self.page.locator(
                        'select[id*="PhoneCountryCode"], select[id*="phoneCountry"]'
                    ).first
                    cc_sel.select_option(value=parsed['country_code'])
                except Exception:
                    pass

            self._fill_phone_segment('area', parsed['area_code'])
            self._fill_phone_segment('number', parsed['number'])
        else:
            # International → Mobile Phone fields
            try:
                self._select_dropdown('Mobile Phone Country Code', parsed['country_code'])
            except Exception:
                pass
            self._fill_by_label('Mobile Phone Area Code', parsed['area_code'])
            self._fill_by_label('Mobile Phone Number',    parsed['number'])

    def _fill_phone_segment(self, segment, value):
        """
        Fill the area code or number input inside the Contact Work Phone row.
        Row input order: [0] country code text  [1] area code  [2] number
        Hyphens are stripped — Oracle wants plain digits.
        """
        if not value:
            return
        # Strip hyphens and spaces for clean digit entry
        value = value.replace('-', '').replace(' ', '')
        # Skip idx 0 (country code) — use 1 for area code, 2 for number
        idx = 1 if segment == 'area' else 2
        try:
            phone_row = self.page.locator(
                'label:has-text("Contact Work Phone")'
            ).locator('xpath=ancestor::tr[1]')
            inputs = phone_row.locator('input')
            if inputs.count() > idx:
                inputs.nth(idx).click()
                inputs.nth(idx).fill(value)
            else:
                logger.warning(f'  Phone row: not enough inputs for segment={segment} (found {inputs.count()})')
        except Exception as e:
            logger.warning(f'  Could not fill phone segment ({segment}): {e}')

    # ------------------------------------------------------------------
    # Helpers — dropdown selection
    # ------------------------------------------------------------------

    def _select_dropdown(self, label_text, value):
        """
        Select a value from an Oracle ADF dropdown (selectOneChoice).
        """
        if not value or not str(value).strip():
            return
        value = str(value).strip()

        try:
            # 1. Click trigger
            trigger = self.page.get_by_label(label_text, exact=False).first
            trigger.wait_for(state='visible', timeout=5000)
            trigger.click()
            self.page.wait_for_timeout(500)

            # 2. Click option
            option = self.page.locator(
                f'[role="option"]:has-text("{value}"), '
                f'li.af_selectItem:has-text("{value}"), '
                f'li:has-text("{value}")'
            ).first
            option.wait_for(state='visible', timeout=5000)
            option.click()
            self.page.wait_for_timeout(300)
            return
        except Exception:
            # Always close lingering popup if selection failed
            try:
                self.page.keyboard.press("Escape")
                self.page.wait_for_timeout(300)
            except Exception:
                pass

        # Fallback: Try typing value into input directly
        try:
            field = self.page.get_by_label(label_text, exact=False).first
            field.click()
            field.fill(value)
            self.page.keyboard.press("Tab")
            self.page.wait_for_timeout(300)
        except Exception as e:
            logger.warning(f"  Could not select '{value}' in '{label_text}': {e}")

    def _search_and_select_existing(self, label_text, search_text):
        """
        Type into an "Existing Account"/"Existing Contact"-style live-search
        field (Oracle ADF's AFAutoSuggestItem widget) and select the first
        real suggestion if one appears -- avoids creating a duplicate
        Account/Contact record when one already exists in Oracle.

        Returns True if a match was selected (caller should then skip filling
        the corresponding "New Account"/"New Contact" field -- confirmed live
        that Oracle auto-mirrors the selected value into it anyway). Returns
        False if there's nothing to search for, no suggestions appeared, or
        anything about the interaction failed -- caller falls back to filling
        the "New X" field as before, exactly like today's behavior.

        Confirmed live against the real tenant: real suggestions are
        `li.AFAutoSuggestItem[role="option"]` with a non-empty `data-afr-value`,
        scoped to the specific visible popup -- an earlier, unscoped version of
        this selector picked up a stale hidden suggestion left over from a
        *different* field's last search, which is why both the popup-visibility
        and non-empty-value checks below are load-bearing, not defensive fluff.
        """
        if not search_text or not str(search_text).strip():
            return False
        search_text = str(search_text).strip()

        try:
            field = self.page.get_by_label(label_text, exact=False).first
            field.click()
            field.fill('')
            field.type(search_text, delay=50)
            self.page.wait_for_timeout(1200)

            popup = self.page.locator('ul.AFAutoSuggestItemsContainer[role="listbox"]:visible').first
            options = popup.locator('li.AFAutoSuggestItem[role="option"]')
            count = options.count()
            match = None
            for i in range(count):
                opt = options.nth(i)
                if (opt.get_attribute('data-afr-value') or '').strip():
                    match = opt
                    break

            if match is None:
                field.fill('')  # clear the typed text so it doesn't linger as stray input
                return False

            match.click()
            self.page.wait_for_timeout(500)
            return True
        except Exception as e:
            logger.warning(f'  Existing-record search for "{label_text}" failed: {e}')
            return False

    # ------------------------------------------------------------------
    # Helpers — error detection
    # ------------------------------------------------------------------

    def _get_popup_error(self):
        """
        Return the text of an Oracle validation error popup if one is visible,
        otherwise return None.
        """
        selectors = [
            '[id*="msgDlg"]',           # ADF message dialog
            '.af_popup',                 # ADF popup
            '[role="dialog"]',           # generic dialog
            '.af_messages',              # inline message area
            '[id*="messages"]',
        ]
        for sel in selectors:
            try:
                el = self.page.locator(sel).first
                if el.is_visible(timeout=1_000):
                    text = el.text_content() or ''
                    if text.strip():
                        return text
            except PWTimeout:
                continue
        return None

    def _dismiss_popup(self):
        """Click OK/Close on an Oracle error popup if present."""
        for btn_text in ('OK', 'Close', 'Yes'):
            try:
                btn = self.page.locator(
                    f'[role="dialog"] button:has-text("{btn_text}"), '
                    f'.af_popup button:has-text("{btn_text}")'
                ).first
                if btn.is_visible(timeout=1_000):
                    btn.click()
                    self.page.wait_for_timeout(500)
                    return
            except PWTimeout:
                continue

    # ------------------------------------------------------------------
    # Helpers — low-level clicks / fills
    # ------------------------------------------------------------------

    def _fill_first_visible(self, selector, value):
        loc = self.page.locator(selector).first
        loc.wait_for(state='visible', timeout=MEDIUM)
        loc.fill(value)

    def _click_first_visible(self, selector):
        loc = self.page.locator(selector).first
        loc.wait_for(state='visible', timeout=MEDIUM)
        loc.click()

    def _js_click_first_visible(self, selector):
        """
        Dispatches a real DOM click() directly on the element instead of a
        Playwright synthetic mouse click. Needed for the Navigator accordion's
        "Sales" row specifically -- confirmed live that Playwright's
        actionability check flags it as intercepted by its own plain
        zero-size wrapper <div> (no id/class, not a real modal/glasspane),
        even though the row is genuinely visible and clickable by a human.
        el.click() bypasses that hit-testing check and still fires whatever
        listener the widget has bound (on the div or via bubbling), which is
        all a human's click would do anyway.
        """
        loc = self.page.locator(selector).first
        loc.wait_for(state='visible', timeout=MEDIUM)
        loc.evaluate('el => el.click()')

    # ------------------------------------------------------------------
    # Screenshot helper (called from main on failure)
    # ------------------------------------------------------------------

    def screenshot(self, filename):
        path = self.log_dir / filename
        try:
            self.page.screenshot(path=str(path), full_page=False)
            logger.info(f'  Screenshot saved: {path}')
        except Exception:
            pass
