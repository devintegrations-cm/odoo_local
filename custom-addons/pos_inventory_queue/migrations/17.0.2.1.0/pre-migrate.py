def migrate(cr, version):
    """Create next_retry_date column if missing.

    The field was added to pos.inventory.queue after the module was
    initially installed.  Odoo ORM normally creates missing columns
    during -u, but a pre-migration script makes the upgrade more
    predictable and avoids a potential ALTER TABLE timeout on tables
    with hundreds of thousands of rows.
    """
    cr.execute("""
        SELECT EXISTS (
            SELECT 1
              FROM information_schema.columns
             WHERE table_name = 'pos_inventory_queue'
               AND column_name  = 'next_retry_date'
        )
    """)
    exists = cr.fetchone()[0]
    if not exists:
        cr.execute(
            "ALTER TABLE pos_inventory_queue "
            "ADD COLUMN next_retry_date TIMESTAMP"
        )
