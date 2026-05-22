"""
Test ipotesi #1: aggirare il bug 'column tipo_documento does not exist' con
context che disabilita il tracking/mail (e quindi il compute display_name).

Target di test: attachment 5351870 (RAJAPACK), in coda registered=False.
Il test tenta write {'registered': True} con vari context e poi rimette False.

NON crea né modifica altri record.
"""
import sys, os, traceback
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from dotenv import load_dotenv
load_dotenv(ROOT / 'config' / 'credentials.env')

from core.odoo_rw_client import OdooReadWriteClient

ATT_ID = 5351870  # RAJAPACK, registered=False ora

client = OdooReadWriteClient(
    url=os.environ['ODOO_URL'], db=os.environ['ODOO_DB'],
    username=os.environ['ODOO_USERNAME'], password=os.environ['ODOO_PASSWORD'])
client.connect()


def show_state(label):
    """Stampa lo stato corrente dell'attachment (senza campi che innescano cascade)."""
    try:
        rec = client._call('fatturapa.attachment.in', 'read', [ATT_ID],
                           fields=['id', 'registered', 'invoices_total',
                                   'invoices_number'])
        if rec:
            print(f"  [{label}] registered={rec[0]['registered']} "
                  f"nf={rec[0]['invoices_number']} tot={rec[0]['invoices_total']}")
    except Exception as e:
        print(f"  [{label}] read FAIL: {str(e)[:150]}")


def try_write(vals, context=None, label=""):
    """Tenta un write sull'attachment con eventuale context. Cattura Fault."""
    print()
    print(f"=== {label} ===")
    print(f"  vals: {vals}")
    print(f"  context: {context!r}")
    try:
        if context is not None:
            # Inietto context tramite kwargs (Odoo execute_kw supporta {'context': ...})
            client._call('fatturapa.attachment.in', 'write',
                         [ATT_ID], vals, context=context)
        else:
            client._call('fatturapa.attachment.in', 'write',
                         [ATT_ID], vals)
        print(f"  >>> OK (write passato)")
        return True
    except Exception as e:
        msg = str(e)
        # Estraggo l'essenziale del Fault Odoo (prime righe)
        first = msg.split('\\n')[0] if '\\n' in msg else msg.split('\n')[0]
        print(f"  >>> FAIL: {first[:300]}")
        if 'tipo_documento does not exist' in msg:
            print(f"      [conferma: è il bug cascade tipo_documento]")
        return False


def reset_attachment():
    """Riporta attachment a registered=False per non lasciarlo in stato strano."""
    print()
    print("--- reset finale: tentativo write registered=False con context completo ---")
    try:
        ctx = {'tracking_disable': True, 'mail_notrack': True,
               'mail_create_nolog': True, 'mail_create_nosubscribe': True}
        client._call('fatturapa.attachment.in', 'write',
                     [ATT_ID], {'registered': False}, context=ctx)
        print("  reset OK")
    except Exception as e:
        print(f"  reset FAIL: {str(e)[:200]}")


print("=" * 80)
print(f"TEST IPOTESI #1 — bypass bug cascade Odoo via context")
print(f"Target: fatturapa.attachment.in id={ATT_ID} (RAJAPACK)")
print("=" * 80)

show_state("PRE")

# Test 1: write SENZA context (baseline — mi aspetto Fault)
ok1 = try_write({'registered': True}, context=None,
                label="TEST 1 — registered=True SENZA context")
show_state("post-T1")

# Reset solo se T1 è passato
if ok1:
    try_write({'registered': False}, context=None,
              label="reset T1 (registered=False senza context)")

# Test 2: context tracking_disable solo
ok2 = try_write({'registered': True},
                context={'tracking_disable': True},
                label="TEST 2 — context={'tracking_disable': True}")
show_state("post-T2")
if ok2:
    try_write({'registered': False},
              context={'tracking_disable': True},
              label="reset T2")

# Test 3: context full anti-mail
ok3 = try_write({'registered': True},
                context={'tracking_disable': True, 'mail_notrack': True,
                         'mail_create_nolog': True, 'mail_create_nosubscribe': True},
                label="TEST 3 — context anti-mail completo")
show_state("post-T3")
if ok3:
    try_write({'registered': False},
              context={'tracking_disable': True, 'mail_notrack': True,
                       'mail_create_nolog': True, 'mail_create_nosubscribe': True},
              label="reset T3")

# Test 4: context anche con no_recompute (forse evita il compute display_name)
ok4 = try_write({'registered': True},
                context={'tracking_disable': True, 'mail_notrack': True,
                         'mail_create_nolog': True, 'mail_create_nosubscribe': True,
                         'no_recompute': True},
                label="TEST 4 — context full + no_recompute")
show_state("post-T4")
if ok4:
    try_write({'registered': False},
              context={'tracking_disable': True, 'mail_notrack': True,
                       'mail_create_nolog': True, 'mail_create_nosubscribe': True,
                       'no_recompute': True},
              label="reset T4")

# Reset finale di sicurezza
reset_attachment()
show_state("FINE")

print()
print("=" * 80)
print(f"RIASSUNTO: T1={ok1}  T2={ok2}  T3={ok3}  T4={ok4}")
print("=" * 80)
