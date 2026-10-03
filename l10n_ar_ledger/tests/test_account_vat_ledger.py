##############################################################################
# For copyright and license notices, see __manifest__.py file in module root
# directory
##############################################################################
from odoo.exceptions import ValidationError
from odoo.tests import tagged

from odoo.addons.l10n_ar.tests.common import TestAr


@tagged("post_install", "-at_install")
class TestAccountVatLedger(TestAr):
    """Regression tests for the digital files field formatting."""

    @classmethod
    def setUpClass(cls, chart_template_ref="l10n_ar.l10nar_ri_chart_template"):
        super().setUpClass(chart_template_ref=chart_template_ref)
        cls.ledger = cls.env["account.vat.ledger"].new(
            {"type": "purchase", "company_id": cls.company_ri.id}
        )

    def _partner(self, vat, responsibility_xmlid, identification_xmlid):
        return self.env["res.partner"].create(
            {
                "name": "Test partner",
                "vat": vat,
                "l10n_ar_afip_responsibility_type_id": self.env.ref(
                    responsibility_xmlid
                ).id,
                "l10n_latam_identification_type_id": self.env.ref(
                    identification_xmlid
                ).id,
            }
        )

    def test_document_number_strips_separators_for_any_responsibility(self):
        """The VAT is stored the way the user types it, with separators.

        The digital files use a fixed position layout: keeping the separators
        shifts every following field of the line and AFIP rejects the file.
        Before this fix only Consumidor Final was sanitized.
        """
        partner = self._partner("30-71429569-8", "l10n_ar.res_IVARI", "l10n_ar.it_cuit")
        self.assertEqual(
            self.ledger.get_partner_document_number(partner),
            "00000000030714295698",
        )
        self.assertEqual(len(self.ledger.get_partner_document_number(partner)), 20)

    def test_document_number_strips_separators_for_consumidor_final(self):
        """The behaviour that already worked must keep working."""
        partner = self._partner("20-22222222-3", "l10n_ar.res_CF", "l10n_ar.it_cuit")
        self.assertEqual(
            self.ledger.get_partner_document_number(partner),
            "00000000020222222223",
        )

    def test_document_number_without_vat_raises(self):
        """An empty VAT used to produce twenty zeros instead of an error."""
        partner = self._partner(False, "l10n_ar.res_IVARI", "l10n_ar.it_cuit")
        with self.assertRaises(ValidationError):
            self.ledger.get_partner_document_number(partner)

    def test_aliquots_count_matches_the_rate_records(self):
        """Field 19 of REGDIGITAL_CV_CBTE must match the number of records
        the same invoice produces in REGDIGITAL_CV_ALICUOTAS.

        Both come from different code paths: the count from `_get_aliquots`
        and the records from the core `_get_vat()`. A tax without VAT AFIP
        code (an IIBB perception) used to be counted here but never produced
        a rate record, so AFIP rejected the pair of files.
        """
        invoice = self.init_invoice(
            "out_invoice",
            partner=self.res_partner_adhoc,
            products=self.product_iva_21,
        )
        perception = self.env["account.tax"].create(
            {
                "name": "Perception IIBB (test)",
                "amount": 3.0,
                "amount_type": "percent",
                "type_tax_use": "sale",
                "company_id": self.company_ri.id,
                "tax_group_id": self.tax_perc_iibb.tax_group_id.id,
            }
        )
        invoice.invoice_line_ids[0].tax_ids = [
            (4, self.tax_21.id),
            (4, perception.id),
        ]
        invoice.action_post()

        self.assertEqual(
            self.ledger._get_aliquots(invoice),
            len(invoice._get_vat()),
            "field 19 must match the number of REGDIGITAL_CV_ALICUOTAS records",
        )
        self.assertEqual(self.ledger._get_aliquots(invoice), 1)

    def _purchase_bill(self, number, lines):
        """Post a vendor bill A with ``lines`` as (product, price, taxes)."""
        doc_type = self.env.ref("l10n_ar.dc_a_f")
        doc_type.export_to_digital = True
        journal = self.env["account.journal"].search(
            [("type", "=", "purchase"), ("company_id", "=", self.company_ri.id)],
            limit=1,
        )
        bill = self.env["account.move"].create(
            {
                "move_type": "in_invoice",
                "journal_id": journal.id,
                "partner_id": self.res_partner_adhoc.id,
                "invoice_date": "2026-06-21",
                "l10n_latam_document_type_id": doc_type.id,
                "l10n_latam_document_number": number,
                "invoice_line_ids": [
                    (
                        0,
                        0,
                        {
                            "product_id": product.id,
                            "quantity": 1.0,
                            "price_unit": price,
                            "tax_ids": [(6, 0, taxes.ids)],
                        },
                    )
                    for product, price, taxes in lines
                ],
            }
        )
        bill.action_post()
        return bill, journal

    def test_purchase_computable_vat_credit_is_the_vat_only(self):
        """Field 21 (Credito Fiscal Computable) without proration is the VAT
        assessed of the voucher (ARCA specification, field 21, positions
        240 to 254): it must not add the taxable base nor the untaxed amounts.
        """
        tax_105 = self._search_tax("iva_105", "purchase")
        bill, journal = self._purchase_bill(
            "00001-00000457",
            [
                (self.product_iva_21, 500.0, self.tax_21_purchase),
                (self.product_iva_105, 1000.0, tax_105),
                (self.product_no_gravado, 200.0, self.tax_no_gravado_purchase),
            ],
        )
        ledger = self.env["account.vat.ledger"].new(
            {
                "type": "purchase",
                "company_id": self.company_ri.id,
                "journal_ids": [(6, 0, journal.ids)],
                "date_from": "2026-06-01",
                "date_to": "2026-06-30",
            }
        )
        # Reading the computed vouchers first is what the form does.
        self.assertTrue(ledger.invoice_ids)
        self.assertEqual(ledger.get_digital_invoices().ids, bill.ids)
        ledger.compute_digital_data()
        rows = ledger.REGDIGITAL_CV_CBTE.split("\r\n")
        self.assertEqual(len(rows), 1)
        self.assertEqual(len(rows[0]), 325)
        # Field 21 sits at positions 240 to 254.
        # 21% on 500.00 plus 10.5% on 1000.00: 105.00 + 105.00 of VAT.
        self.assertEqual(rows[0][239:254], "000000000021000")
