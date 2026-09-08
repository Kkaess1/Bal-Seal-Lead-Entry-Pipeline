"""Read-only daily enrichment of local leads from Oracle Fusion Sales.

The operational submission client intentionally stays separate.  This module
only performs GET requests and writes reporting snapshots to SQLite.
"""

import argparse
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

import db
import secrets_store
from oracle_api import API_VERSION, OracleApiError


ROOT = Path(__file__).resolve().parent
logger = logging.getLogger(__name__)

LEAD_FIELDS = ','.join((
    'LeadId', 'LeadNumber', 'StatusCode', 'QualifiedDateTime', 'QualifiedDate',
    'ConvertedDateTime', 'QualificationScore', 'EligibleForConversionFlag',
    'DealAmount', 'CurrencyCode', 'OwnerPartyName', 'LastUpdateDate',
))
OPPORTUNITY_FIELDS = ','.join((
    'OptyId', 'OptyNumber', 'OptyName', 'OptyAccount', 'OptyStatus',
    'OptyCloseDate', 'SalesStageName', 'WinProbability', 'RevenueAmount',
    'ExpectedAmount', 'Currency',
))
ORDER_LINE_FIELDS = ','.join((
    'OptyId', 'OrderLineId', 'OrderNumber', 'OrderLineNumber', 'Description',
    'LineStatusCode', 'WinLossStatusCode', 'OrderDate', 'Amount',
    'ContractValue', 'CurrencyCode',
))


def _utc_now():
    return datetime.now(timezone.utc).isoformat()


class OracleSalesReportingClient:
    def __init__(self, config, username, password, session=None):
        self.base_url = config['base_url'].rstrip('/')
        self.timeout = int(config.get('api_timeout_seconds', 60))
        self.username = username
        self.password = password
        self.session = session or requests.Session()

    @property
    def leads_url(self):
        return f'{self.base_url}/crmRestApi/resources/{API_VERSION}/leads'

    def _get_json(self, url, params=None):
        try:
            response = self.session.get(
                url, params=params, auth=(self.username, self.password),
                headers={'Accept': 'application/json'}, timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise OracleApiError(f'Oracle reporting request failed: {exc}') from exc
        if response.status_code >= 400:
            raise OracleApiError(
                f'Oracle reporting request failed ({response.status_code}): {response.text}',
                status_code=response.status_code, response_text=response.text,
            )
        try:
            return response.json()
        except (ValueError, AttributeError) as exc:
            raise OracleApiError('Oracle reporting request returned invalid JSON') from exc

    def get_lead_outcome(self, oracle_lead_id):
        lead_url = f'{self.leads_url}/{oracle_lead_id}'
        lead = self._get_json(
            lead_url, {'fields': LEAD_FIELDS, 'onlyData': 'true'},
        )
        opportunities = []
        offset = 0
        while True:
            page = self._get_json(
                f'{lead_url}/child/LeadOpportunity',
                {
                    'fields': OPPORTUNITY_FIELDS, 'onlyData': 'true',
                    'limit': '100', 'offset': str(offset),
                },
            )
            items = page.get('items') or []
            opportunities.extend(items)
            if not page.get('hasMore'):
                break
            offset += int(page.get('limit') or len(items) or 100)
        order_lines = self.get_order_lines(
            {item.get('OptyId') for item in opportunities if item.get('OptyId')}
        )
        return lead, opportunities, order_lines

    def get_order_lines(self, opportunity_ids):
        """Order/quote lines linked to these opportunities, or None if unavailable."""
        order_lines = []
        url = f'{self.base_url}/crmRestApi/resources/{API_VERSION}/salesOrderLines'
        for opportunity_id in sorted(opportunity_ids, key=str):
            offset = 0
            while True:
                try:
                    page = self._get_json(
                        url,
                        {
                            'q': f'OptyId={opportunity_id}', 'fields': ORDER_LINE_FIELDS,
                            'onlyData': 'true', 'limit': '100', 'offset': str(offset),
                        },
                    )
                except OracleApiError as exc:
                    if exc.status_code in (403, 404):
                        logger.warning(
                            'Oracle salesOrderLines is unavailable to this API user; '
                            'linked order value will not be refreshed.'
                        )
                        return None
                    raise
                items = page.get('items') or []
                order_lines.extend(items)
                if not page.get('hasMore'):
                    break
                offset += int(page.get('limit') or len(items) or 100)
        return order_lines


def _snapshot(lead, oracle_lead_id, synced_at):
    return {
        'oracle_lead_id': lead.get('LeadId') or oracle_lead_id,
        'oracle_lead_number': lead.get('LeadNumber'),
        'oracle_status': lead.get('StatusCode'),
        'qualified_at': lead.get('QualifiedDateTime') or lead.get('QualifiedDate'),
        'converted_at': lead.get('ConvertedDateTime'),
        'qualification_score': lead.get('QualificationScore'),
        'eligible_for_conversion': lead.get('EligibleForConversionFlag'),
        'deal_amount': lead.get('DealAmount'),
        'deal_currency': lead.get('CurrencyCode'),
        'oracle_owner': lead.get('OwnerPartyName'),
        'oracle_last_updated_at': lead.get('LastUpdateDate'),
        'last_synced_at': synced_at,
    }


def _opportunity(item):
    return {
        'oracle_opportunity_id': item.get('OptyId'),
        'opportunity_number': item.get('OptyNumber'),
        'name': item.get('OptyName'),
        'account_name': item.get('OptyAccount'),
        'status': item.get('OptyStatus'),
        'sales_stage': item.get('SalesStageName'),
        'close_date': item.get('OptyCloseDate'),
        'win_probability': item.get('WinProbability'),
        'revenue_amount': item.get('RevenueAmount'),
        'expected_amount': item.get('ExpectedAmount'),
        'currency_code': item.get('Currency'),
    }


def _order_line(item):
    return {
        'oracle_opportunity_id': item.get('OptyId'),
        'order_line_id': item.get('OrderLineId'),
        'order_number': item.get('OrderNumber'),
        'order_line_number': item.get('OrderLineNumber'),
        'product_name': item.get('Description'),
        'line_status': item.get('LineStatusCode'),
        'win_loss_status': item.get('WinLossStatusCode'),
        'order_date': item.get('OrderDate'),
        'net_amount': item.get('Amount'),
        'contract_value': item.get('ContractValue'),
        'currency_code': item.get('CurrencyCode'),
    }


def sync_all(client, candidates=None):
    candidates = candidates if candidates is not None else db.list_oracle_sync_candidates()
    result = {'attempted': len(candidates), 'synced': 0, 'failed': 0}
    db.set_run_state('oracle_sync_status', 'running')
    db.set_run_state('oracle_sync_started_at', _utc_now())
    for local_lead in candidates:
        lead_id = local_lead['id']
        oracle_id = local_lead['oracle_record_id']
        try:
            lead, opportunities, order_lines = client.get_lead_outcome(oracle_id)
            synced_at = _utc_now()
            db.save_oracle_snapshot(
                lead_id, _snapshot(lead, oracle_id, synced_at),
                [_opportunity(item) for item in opportunities],
                None if order_lines is None else [_order_line(item) for item in order_lines],
            )
            if order_lines is None:
                db.set_run_state('oracle_order_lines_status', 'unavailable')
            elif opportunities:
                db.set_run_state('oracle_order_lines_status', 'available')
            result['synced'] += 1
        except Exception as exc:
            if isinstance(exc, OracleApiError) and exc.status_code == 404:
                logger.warning(
                    'Oracle lead %s for local lead %s no longer exists or is not visible.',
                    oracle_id, lead_id,
                )
            else:
                logger.exception('Oracle outcome sync failed for local lead %s', lead_id)
            db.mark_oracle_sync_error(lead_id, oracle_id, exc)
            result['failed'] += 1
    finished_at = _utc_now()
    result['finished_at'] = finished_at
    db.set_run_state('oracle_sync_finished_at', finished_at)
    db.set_run_state('oracle_sync_status', 'ok' if not result['failed'] else 'partial')
    db.set_run_state('oracle_sync_result', json.dumps(result, sort_keys=True))

    lease_token = os.environ.get('PIPELINE_ORACLE_SYNC_LEASE_TOKEN')
    if lease_token:
        db.update_run_lease(
            'job:manual_oracle_sync', lease_token,
            'succeeded' if not result['failed'] else 'failed',
            json.dumps(result),
        )
    return result


def build_client(config_path=ROOT / 'config.json'):
    load_dotenv(ROOT / '.env', override=False)
    with open(config_path, encoding='utf-8') as handle:
        config = json.load(handle).get('oracle', {})
    username = secrets_store.get_secret('ORACLE_USERNAME')
    password = secrets_store.get_secret('ORACLE_PASSWORD')
    if not username or not password:
        raise RuntimeError(
            'Oracle credentials are missing. Store ORACLE_USERNAME and '
            'ORACLE_PASSWORD with secrets_store.py or in .env.'
        )
    return OracleSalesReportingClient(config, username, password)


def main():
    parser = argparse.ArgumentParser(description='Sync lead outcomes from Oracle Sales')
    parser.add_argument('--lead-id', type=int, help='sync one local lead only')
    args = parser.parse_args()
    log_dir = ROOT / 'logs'
    log_dir.mkdir(exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s %(levelname)s %(name)s: %(message)s',
        handlers=[
            logging.FileHandler(log_dir / 'oracle_sync.log', encoding='utf-8'),
            logging.StreamHandler(),
        ],
    )
    db.init_db()
    candidates = None
    if args.lead_id:
        lead = db.get_lead(args.lead_id)
        if not lead or not lead.get('oracle_record_id'):
            raise SystemExit('Local lead not found or has no Oracle record ID.')
        candidates = [lead]
    result = sync_all(build_client(), candidates)
    print(json.dumps(result, indent=2))
    # A missing/permission-limited individual record is captured on that lead
    # and must not make Task Scheduler retry the entire daily batch.  Fail the
    # process only when Oracle returned no usable data at all.
    raise SystemExit(1 if result['attempted'] and not result['synced'] else 0)


if __name__ == '__main__':
    main()
