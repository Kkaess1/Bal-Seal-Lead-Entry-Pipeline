"""
Dev-only helper: populate pipeline.db with representative fake leads so the
dashboard can be built/tested before email ingestion and fraud screening exist.

Every lead here also gets a full fake email body (From/To/Date/Subject + body text)
cached to email_cache/, wired up via messages.body_cache_path, so the dashboard's
"view original email" expand can be built/tested against something realistic.

lead_details is set to the (near-)verbatim email body, not an AI summary -- it's
what actually lands in Oracle's "Lead Details" field, so a sales rep should see
what the customer wrote, not a paraphrase that could drop or distort something.
Extraction still identifies structured fields (name, org, phone, etc.) separately;
it just doesn't compress the body text.

Usage:  py seed_test_leads.py [--reset]
"""

import sys
from pathlib import Path

import db
import territory_map

EMAIL_CACHE_DIR = Path(__file__).parent / 'email_cache'

SAMPLE_LEADS = [
    # Clean lead — no red flags.
    dict(
        lead=dict(
            first_name='Maria', last_name='Chen', organization='Redline Hydraulics',
            title='Senior Design Engineer', phone='281-997-3300',
            email='mchen@redlinehydraulics.com',
            city='Houston', state='TX', zip='77040', country='USA',
            rank='Hot',
        ),
        subject='Custom rotary seal quote request',
        received_at='2026-07-20T09:14:00-05:00',
        raw_body=(
            'Hi,\n\n'
            "I'm working on a hydraulic actuator design and need a custom rotary seal. "
            'Specs are below:\n\n'
            '- Bore diameter: 2.5 in\n'
            '- Operating pressure: 3000 PSI\n'
            '- Temperature range: -20F to 250F\n'
            '- Fluid: synthetic hydraulic oil\n\n'
            "We're targeting a Q3 production start at roughly 5,000 units/year. "
            'Could you send a quote and let us know if you need a drawing from our '
            'side to review tolerances?\n\n'
            'Thanks,\n'
            'Maria Chen\n'
            'Senior Design Engineer\n'
            'Redline Hydraulics\n'
            '281-997-3300'
        ),
        fraud_risk='clear', fraud_reasons=[],
        extraction_confidence=0.95,
    ),
    # Personal email domain.
    dict(
        lead=dict(
            first_name='Dave', last_name='Torres', organization='Torres Fabrication',
            title='Owner', phone='713-555-0148', email='davetorres1982@gmail.com',
            city='Pasadena', state='TX', country='USA',
            rank='Warm',
        ),
        subject='O-ring pricing',
        received_at='2026-07-20T07:52:00-05:00',
        raw_body=(
            'Hello,\n\n'
            "Need pricing on o-rings for a pump rebuild we're doing. Viton material, "
            '3 inch ID, will need about 200 units to start.\n\n'
            'Let me know what you need from me.\n\n'
            'Dave Torres\n'
            'Torres Fabrication\n'
            '713-555-0148'
        ),
        fraud_risk='review',
        fraud_reasons=[
            {'tag': 'personal_email_domain', 'label': 'Personal email domain (gmail.com)',
             'detail': 'Sender address uses a free consumer email provider rather than a company domain.'},
        ],
        extraction_confidence=0.88,
    ),
    # Poor spelling/grammar.
    dict(
        lead=dict(
            first_name='Igor', last_name='Petrov', organization='Petrov Industrial Supply',
            title='Purchasing', phone='+1-330-555-0199', email='ipetrov@petrovsupply.com',
            city='Akron', state='OH', country='USA',
            rank='Warm',
        ),
        subject='seal price',
        received_at='2026-07-19T23:03:00-05:00',
        raw_body=(
            'hi\n\n'
            'we need seal for machine plz send prise list we buy alot thx\n\n'
            'igor\n'
            'petrov industrial supply'
        ),
        fraud_risk='review',
        fraud_reasons=[
            {'tag': 'poor_spelling', 'label': 'Poor spelling/grammar',
             'detail': 'Example: "send prise list we buy alot thx" — multiple spelling/grammar errors in the message body.'},
        ],
        extraction_confidence=0.6,
    ),
    # No engineering specs.
    dict(
        lead=dict(
            first_name='Karen', last_name='Wu', organization='Wu Manufacturing Group',
            title='Buyer', phone='214-555-0173', email='kwu@wumfg.com',
            city='Dallas', state='TX', country='USA',
            rank='Cold',
        ),
        subject='Catalog request',
        received_at='2026-07-17T14:41:00-05:00',
        raw_body=(
            'Hello,\n\n'
            'Please send your catalog and pricing sheet.\n\n'
            'Thank you.\n\n'
            'Karen Wu\n'
            'Buyer, Wu Manufacturing Group'
        ),
        fraud_risk='review',
        fraud_reasons=[
            {'tag': 'no_engineering_specs', 'label': 'No engineering specs',
             'detail': 'Message requests a generic catalog/price list with no dimensions, materials, or application details.'},
        ],
        extraction_confidence=0.7,
    ),
    # Gibberish content.
    dict(
        lead=dict(
            first_name='xk29', last_name='qqzz', organization='asdfgh llc',
            phone=None, email='xk29qqzz@mailinator.com',
        ),
        subject='!!! seal deal today only !!!',
        received_at='2026-07-18T03:12:00-05:00',
        raw_body=(
            'seal product buy now click here amazing deal seal ring buy fast!!! xzy123 '
            'limited time seal offer act now www.totally-real-seals.example.com'
        ),
        fraud_risk='reject',
        fraud_reasons=[
            {'tag': 'gibberish_content', 'label': 'Gibberish/bot-generated content',
             'detail': 'Low real-word ratio and incoherent phrasing consistent with spam/bot submission.'},
            {'tag': 'personal_email_domain', 'label': 'Disposable email domain (mailinator.com)',
             'detail': 'Sender domain is a known disposable/temporary email provider.'},
        ],
        extraction_confidence=0.3,
    ),
    # Multiple flags stacked, still routed to review (never auto-rejected on its own).
    dict(
        lead=dict(
            first_name='sam', last_name='r', organization=None,
            phone=None, email='samr99@yahoo.com',
        ),
        subject='seals',
        received_at='2026-07-19T20:30:00-05:00',
        raw_body='hi do u sell seals we need some for our stuff let me know price',
        fraud_risk='review',
        fraud_reasons=[
            {'tag': 'personal_email_domain', 'label': 'Personal email domain (yahoo.com)',
             'detail': 'Sender address uses a free consumer email provider rather than a company domain.'},
            {'tag': 'no_engineering_specs', 'label': 'No engineering specs',
             'detail': 'No dimensions, materials, or application details mentioned.'},
            {'tag': 'poor_spelling', 'label': 'Poor spelling/grammar',
             'detail': 'Example: "do u sell seals we need some for our stuff" — informal/ungrammatical phrasing.'},
        ],
        extraction_confidence=0.55,
    ),
    # International lead -- demonstrates the confident (non-US) side of territory
    # assignment, since all the US samples above will show "not yet configured".
    dict(
        lead=dict(
            first_name='Lukas', last_name='Weber', organization='Weber Praezisionstechnik GmbH',
            title='Head of Procurement', phone='+49-711-555-0142',
            email='l.weber@weber-praezision.de',
            city='Stuttgart', country='Germany',
            rank='Hot',
        ),
        subject='Anfrage: Custom-Dichtung fuer Hydraulikzylinder',
        received_at='2026-07-20T04:20:00-05:00',
        raw_body=(
            'Guten Tag,\n\n'
            'we are looking for a custom seal for a hydraulic cylinder application. '
            'Rod diameter 60mm, operating pressure 250 bar, temperature range -10C to '
            '120C, medium is mineral oil (HLP 46). Annual volume approx. 8,000 pieces.\n\n'
            'Please send a quotation and lead time. We can provide a technical drawing '
            'on request.\n\n'
            'Best regards,\n'
            'Lukas Weber\n'
            'Head of Procurement\n'
            'Weber Praezisionstechnik GmbH\n'
            '+49 711 555 0142'
        ),
        fraud_risk='clear', fraud_reasons=[],
        extraction_confidence=0.93,
    ),
]


def _write_email_cache(index, sample):
    """
    Cache just the message body text, the way email_ingest.py will later cache
    real Graph message bodies. Headers (From/Subject/Date) live structurally on
    the messages row, not in this file — mirrors how Graph's API actually
    separates headers from body, and avoids the dashboard showing them twice.
    """
    EMAIL_CACHE_DIR.mkdir(exist_ok=True)
    path = EMAIL_CACHE_DIR / f'seed-msg-{index}.txt'
    path.write_text(sample['raw_body'], encoding='utf-8')
    return str(path)


def seed(reset=False):
    if reset and db.DB_PATH.exists():
        db.DB_PATH.unlink()
    db.init_db()

    conn = db.get_connection()
    try:
        for i, sample in enumerate(SAMPLE_LEADS, 1):
            body_cache_path = _write_email_cache(i, sample)
            sample['lead']['lead_details'] = sample['raw_body']
            msg_id = db.insert_message(
                source_message_id=f'seed-msg-{i}',
                received_at=sample['received_at'],
                sender_email=sample['lead'].get('email'),
                sender_name=f"{sample['lead'].get('first_name') or ''} {sample['lead'].get('last_name') or ''}".strip(),
                subject=sample['subject'],
                body_cache_path=body_cache_path,
                conn=conn,
            )
            lead_id = db.insert_lead(
                sample['lead'], source='seed_test', message_id=msg_id, conn=conn,
            )
            tm, basis = territory_map.assign_tm(sample['lead'])
            db.set_territory_assignment(lead_id, tm, basis, conn=conn)
            db.set_extraction_result(
                lead_id, sample['extraction_confidence'],
                'Seeded test data — extraction_notes would normally explain the model\'s reasoning here.',
                conn=conn,
            )
            db.set_fraud_screen_result(
                lead_id, sample['fraud_risk'], sample['fraud_reasons'], None, conn=conn,
            )
        conn.commit()
    finally:
        conn.close()

    print(f'Seeded {len(SAMPLE_LEADS)} test leads (with cached email bodies) into {db.DB_PATH}')


if __name__ == '__main__':
    seed(reset='--reset' in sys.argv)
