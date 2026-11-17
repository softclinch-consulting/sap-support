"""Demo data: three client companies, an AMS team, realistic SAP tickets and KB articles.

Run once on an empty database:  python app.py --demo
All demo accounts use the password  Demo@1234
"""
from datetime import timedelta

from werkzeug.security import generate_password_hash

import app as hd

PASSWORD = "Demo@1234"


def seed():
    hd.init_db()
    db = hd.connect()
    if db.execute("SELECT COUNT(*) FROM tickets").fetchone()[0]:
        print("Database already has tickets; demo data not loaded.")
        db.close()
        return
    now = hd.utcnow()
    ago = lambda **kw: now - timedelta(**kw)  # noqa: E731

    companies = {}
    for name, contact, release in [
        ("Nilgiri Textiles Ltd", "Ravi Kumar", "SAP ECC 6.0 EHP8"),
        ("Vegam Auto Components", "Anita Desai", "SAP S/4HANA 2022"),
        ("Meridia Lifesciences", "Farhan Ali", "SAP S/4HANA 2023"),
    ]:
        cur = db.execute("INSERT INTO companies(name,contact_name,sap_release,created_at) VALUES(?,?,?,?)",
                         (name, contact, release, hd.iso(ago(days=60))))
        companies[name.split()[0]] = cur.lastrowid

    users = {}

    def add_user(key, name, email, role, company=None, modules=""):
        cur = db.execute("INSERT INTO users(name,email,password_hash,role,company_id,modules,created_at) "
                         "VALUES(?,?,?,?,?,?,?)",
                         (name, email, generate_password_hash(PASSWORD), role,
                          companies.get(company), modules, hd.iso(ago(days=60))))
        users[key] = db.execute("SELECT * FROM users WHERE id=?", (cur.lastrowid,)).fetchone()

    add_user("priya", "Priya Raman", "admin@demo.local", "admin")
    add_user("arjun", "Arjun Mehta", "arjun@demo.local", "consultant", modules="FI,CO")
    add_user("kavya", "Kavya Iyer", "kavya@demo.local", "consultant", modules="MM,SD,WM/EWM,QM")
    add_user("rahul", "Rahul Nair", "rahul@demo.local", "consultant", modules="ABAP,Basis,Security,Integration,Fiori,BW/BI")
    add_user("sneha", "Sneha Kulkarni", "sneha@demo.local", "consultant", modules="HCM,PP,PM,PS,SuccessFactors")
    add_user("ravi", "Ravi Kumar", "ravi@nilgiri.demo", "client", "Nilgiri")
    add_user("anita", "Anita Desai", "anita@vegam.demo", "client", "Vegam")
    add_user("farhan", "Farhan Ali", "farhan@meridia.demo", "client", "Meridia")
    client_of = {"Nilgiri": "ravi", "Vegam": "anita", "Meridia": "farhan"}

    def ticket(company, created, events=(), **f):
        f.setdefault("category", "Incident")
        f.setdefault("sap_system", "PRD")
        tid = hd.create_ticket(db, f, companies[company], users[client_of[company]]["id"], created)
        for ev in events:
            kind, delta = ev[0], ev[1]
            at = created + delta
            if kind == "comment":
                hd.add_comment(db, tid, users[ev[2]], ev[3], ev[4] if len(ev) > 4 else False, at)
            elif kind == "status":
                hd.set_status(db, tid, ev[3], users[ev[2]]["id"], at)
            elif kind == "set":
                t = hd.reload_ticket(db, tid)
                hd.save_ticket(db, t, ev[3], users[ev[2]]["id"], at)
        return tid

    m = lambda minutes: timedelta(minutes=minutes)  # noqa: E731

    ticket("Nilgiri", ago(hours=3, minutes=10), [
        ("comment", m(18), "arjun", "Looking into it now. Can you confirm the run date and identification you used in F110?"),
        ("status", m(19), "arjun", "In Progress"),
        ("comment", m(40), "ravi", "Run date 17.11.2026, identification NT01. Same variant worked last week."),
        ("comment", m(75), "arjun", "Vendor master for 14 vendors has payment method T removed after last week's mass change (XK99). Checking change log with S_ALR_87010040.", True),
    ], title="F110 payment run not picking open vendor items", module="FI", priority="P1",
        sap_client="300", tcode="F110",
        error_message="No valid payment method found (exception list in payment proposal log)",
        description="Today's vendor payment run proposal shows zero items for company code 1000. "
                    "About 120 vendor invoices are due. Bank file must be sent by 4 PM.",
        business_impact="Vendor payments for the week are blocked; bank cut-off at 4 PM today.",
        users_affected="Accounts payable team (6 users)")

    ticket("Vegam", ago(hours=6), [
        ("comment", m(35), "kavya", "Stock for material 100045 in plant 1000 / sloc 0001 shows 0 EA in MMBE. "
                                    "A 311 transfer was posted to sloc 0002 yesterday. Can you confirm which storage location the GR should go to?"),
        ("status", m(36), "kavya", "Awaiting Customer"),
    ], title="Goods issue fails with deficit of stock error", module="MM", priority="P2",
        sap_client="100", tcode="MIGO",
        error_message="M7 021 Deficit of SL Unrestricted-use 10 EA : 100045 1000 0001",
        description="Goods issue to production order 1000234 fails in MIGO with a deficit of stock message. "
                    "Stores team says physical stock is available.",
        business_impact="Assembly line 2 waiting for components.", users_affected="Stores (3 users)")

    ticket("Meridia", ago(hours=12), [
        ("comment", m(50), "rahul", "IDocs fail because customer 300987 has no sales area data for 1000/10/00. "
                                    "Checking with your master data team whether the sales area extension was missed."),
        ("status", m(51), "rahul", "In Progress"),
        ("comment", m(240), "rahul", "47 IDocs affected. Will reprocess via BD87 once master data is extended.", True),
    ], title="Sales orders from distributor portal stuck in IDoc status 51", module="Integration", priority="P2",
        sap_client="200", tcode="WE02",
        error_message="Status 51: Application document not posted - Sales area 1000 10 00 is not defined for customer 300987",
        description="Orders from the distributor portal since yesterday evening are not creating sales orders. "
                    "ORDERS05 IDocs are in status 51.",
        business_impact="Distributor orders not visible to dispatch; same-day shipments at risk.")

    ticket("Nilgiri", ago(days=2, hours=3), [
        ("status", m(60), "rahul", "In Progress"),
        ("comment", m(65), "rahul", "Role Z_MM_BUYER_DISPLAY assigned in PFCG for user SMEHTA, valid until 31.12.9999."),
        ("set", m(70), "rahul", {"resolution": "Assigned role Z_MM_BUYER_DISPLAY to user SMEHTA after approval from the MM process owner.",
                                 "root_cause": "New joiner; access request not raised at onboarding."}),
        ("status", m(71), "rahul", "Resolved"),
    ], title="Display access to purchase orders (ME23N) for new buyer", module="Security",
        category="Authorization / Access", priority="P3", sap_client="300", tcode="ME23N",
        error_message="You are not authorized to use transaction ME23N",
        description="New buyer Sanjay Mehta (user SMEHTA) needs display access to purchase orders. Approved by MM process owner.")

    ticket("Vegam", ago(hours=7), [
        ("comment", m(25), "rahul", "Dump caused by a SELECT without a package size on MSEG for the full date range. "
                                    "Fix moved to QAS for testing in transport VEDK900123."),
        ("status", m(26), "rahul", "In Progress"),
        ("set", m(240), "rahul", {"transport_request": "VEDK900123",
                                  "root_cause": "Custom report reads all MSEG records into one internal table."}),
        ("status", m(241), "rahul", "In Testing"),
        ("comment", m(242), "rahul", "Please test in QAS client 200 with plant 1000 and the full year selection."),
    ], title="Short dump TSV_TNEW_PAGE_ALLOC_FAILED in stock ageing report", module="ABAP", priority="P2",
        sap_client="100", tcode="ST22", error_message="Runtime error TSV_TNEW_PAGE_ALLOC_FAILED in program ZMM_STOCK_AGEING",
        description="Month-end stock ageing report ZMM_STOCK_AGEING dumps when run for the full year.")

    ticket("Meridia", ago(minutes=30), [], title="Payroll simulation error for November 2026",
           module="HCM", priority="P2", sap_client="200", tcode="PC00_M40_CALC",
           error_message="Personnel number 10023: Wage type 1000 not permitted for date",
           description="Payroll simulation for payroll area IN shows errors for 18 employees after the salary revision upload.",
           business_impact="November payroll must be released by 25th.")

    ticket("Nilgiri", ago(hours=1), [], title="Request: SAP GUI 8.00 installation guide for new laptops",
           module="Other", category="Service Request", priority="P4", sap_system=None,
           description="IT is rolling out 15 new laptops. Please share the SAP GUI 8.00 installation guide and connection entries.")

    ticket("Vegam", ago(hours=20), [
        ("comment", m(90), "rahul", "Spool work processes are fine; issue reproduces only on the output server for device ZPR1. "
                                    "Raised a case with SAP through the support portal with traces attached."),
        ("status", m(91), "rahul", "Awaiting SAP"),
    ], title="Spool requests stuck in Waiting status for warehouse printer", module="Basis", priority="P3",
        sap_client="100", tcode="SP01", error_message="Spool request status: Waiting",
        description="Delivery notes sent to printer ZPR1 stay in Waiting status since this morning.")

    ticket("Meridia", ago(days=2), [
        ("status", m(120), "arjun", "In Progress"),
        ("comment", m(125), "arjun", "Draft hierarchy shared on email. Will create in DEV and move to QAS after your approval."),
    ], title="New cost center group for R&D reporting", module="CO", category="Change Request",
        priority="P4", sap_system="DEV", tcode="KSH1",
        description="Create cost center group RND_ALL with sub-groups for formulation, analytical and clinical R&D.")

    ticket("Nilgiri", ago(days=10), [
        ("comment", m(12), "arjun", "Period 11 opened for account types +, D, K, S in variant 1000 via OB52. Please retry."),
        ("set", m(13), "arjun", {"resolution": "Opened posting period 11/2026 in OB52 for variant 1000.",
                                 "root_cause": "Period opening step missed in month-start checklist."}),
        ("status", m(13), "arjun", "Resolved"),
        ("status", m(40), "ravi", "Closed"),
    ], title="Posting period 11/2026 not open for company code 1000", module="FI", priority="P2",
        sap_client="300", tcode="FB60", error_message="F5 201 Posting period 011 2026 is not open",
        description="Unable to post vendor invoices from 1st November.")

    ticket("Vegam", ago(days=4), [
        ("comment", m(300), "rahul", "Cache cleared and target mapping for TaskProcessing re-activated."),
        ("set", m(1800), "rahul", {"resolution": "Re-activated the My Inbox target mapping and cleared the Fiori front-end cache "
                                                 "(/UI2/INVALIDATE_GLOBAL_CACHES).",
                                   "root_cause": "Catalog transport overwrote the target mapping."}),
        ("status", m(1801), "rahul", "Resolved"),
    ], title="My Inbox Fiori tile not loading for approvers", module="Fiori", priority="P3",
        sap_system="PRD", tcode="/UI2/FLP", error_message="Could not open app. Please try again later.",
        description="Approvers see an error when opening My Inbox from the launchpad since this morning.")

    ticket("Meridia", ago(days=4), [
        ("status", m(30), "kavya", "In Progress"),
        ("set", m(400), "kavya", {"resolution": "Activated inspection type 01 for the material in the QM view."}),
        ("status", m(401), "kavya", "Resolved"),
        ("comment", m(2000), "farhan", "Reopened: inspection lots are still not created for batches received today."),
        ("status", m(2001), "farhan", "In Progress"),
    ], title="Inspection lot not created for goods receipt of batch material", module="QM", priority="P3",
        sap_client="200", tcode="QA32",
        description="GR for raw material RM-2231 with batch does not create an inspection lot.")

    ticket("Nilgiri", ago(days=15), [
        ("status", m(200), "sneha", "In Progress"),
        ("set", m(900), "sneha", {"resolution": "Shared the steps to export via List > Export > Spreadsheet and set up the layout ZPM_OPEN."}),
        ("status", m(901), "sneha", "Resolved"),
        ("status", m(1500), "ravi", "Closed"),
    ], title="Export maintenance order list to Excel", module="PM", category="Service Request", priority="P4",
        tcode="IW39", description="Need to export open maintenance orders with planner group to Excel weekly.")

    ticket("Vegam", ago(days=20), [
        ("status", m(45), "kavya", "In Progress"),
        ("set", m(300), "kavya", {"resolution": "Credit limit increased in UKM_BP after finance approval and orders released in VKM3."}),
        ("status", m(301), "kavya", "Resolved"),
        ("status", m(600), "anita", "Closed"),
    ], title="Sales orders on credit block for customer 200145", module="SD", priority="P3",
        sap_client="100", tcode="VKM3", description="All new orders for customer 200145 are blocked for credit.")

    kb = [
        ("Posting period is not open (F5 201)", "FI", "OB52", "public", "posting period, month start",
         "Posting fails with message F5 201 Posting period XXX YYYY is not open, usually on the first working day of a month.",
         "1. Run OB52 and find the posting period variant for the company code (OBY6 shows the assignment).\n"
         "2. Open the new period for the required account types (+, A, D, K, M, S).\n"
         "3. Close the oldest period you no longer need, following your finance close policy.\n"
         "4. Ask the user to repost. Add period opening to the month-start checklist."),
        ("Deficit of stock (M7 021) on goods movement", "MM", "MIGO", "public", "stock, goods issue, goods receipt",
         "MIGO or another goods movement fails with M7 021 Deficit of SL Unrestricted-use for a material, plant and storage location.",
         "1. Check stock for the exact plant, storage location and batch in MMBE.\n"
         "2. Check the posting date: stock may exist today but not on an earlier posting date (MB5B).\n"
         "3. Look for recent transfer postings (311) or reversals that moved the stock.\n"
         "4. Post the movement from the storage location that holds the stock, or transfer it first.\n"
         "Do not activate negative stock as a workaround without the process owner's approval."),
        ("IDoc in status 51: analyse and reprocess", "Integration", "BD87", "public", "idoc, interface, edi",
         "Inbound IDocs (for example ORDERS05) stay in status 51 Application document not posted.",
         "1. Open the IDoc in WE02 and read the status 51 message text.\n"
         "2. Fix the cause, usually missing master data (customer sales area, material, partner profile).\n"
         "3. Reprocess the IDocs in BD87 (select status 51, Process).\n"
         "4. If many IDocs failed, schedule RBDMANI2 for the selection."),
        ("Requesting access to a transaction", "Security", "SU53", "public", "authorization, access, role",
         "A user sees 'You are not authorized to use transaction ...'.",
         "1. Ask the user to run /nSU53 immediately after the error and attach a screenshot to the ticket.\n"
         "2. Get approval from the process owner for the role that covers the transaction.\n"
         "3. Security consultant assigns the role in SU01/PFCG and records it in the ticket."),
        ("Analysing short dumps with ST22", "ABAP", "ST22", "internal", "dump, runtime error, memory",
         "A program terminates with a runtime error such as TSV_TNEW_PAGE_ALLOC_FAILED or DBSQL_DUPLICATE_KEY_ERROR.",
         "1. Open ST22, filter by user and date, and read 'What happened' and 'Source code extract'.\n"
         "2. For memory errors, check for SELECT statements without package size or unrestricted selections.\n"
         "3. Reproduce in QAS, fix, and move the transport with the ticket number in the description.\n"
         "4. Record the transport request on the ticket."),
    ]
    for i, (title, module, tcode, vis, tags, symptoms, solution) in enumerate(kb):
        ts = hd.iso(ago(days=30 - i))
        db.execute("INSERT INTO kb_articles(title,module,tcode,symptoms,solution,tags,visibility,author_id,views,created_at,updated_at) "
                   "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                   (title, module, tcode, symptoms, solution, tags, vis, users["arjun" if module == "FI" else "rahul"]["id"],
                    12 + i * 7, ts, ts))
    db.commit()
    db.close()
    print("Demo data loaded. Sign in with any of these (password %s):" % PASSWORD)
    for email, role in [("admin@demo.local", "admin"), ("arjun@demo.local", "consultant FI/CO"),
                        ("kavya@demo.local", "consultant MM/SD"), ("rahul@demo.local", "consultant ABAP/Basis"),
                        ("ravi@nilgiri.demo", "client - Nilgiri Textiles")]:
        print(f"  {email:24} {role}")


if __name__ == "__main__":
    seed()
