# -*- coding: utf-8 -*-
from odoo import api, fields, models
from odoo.exceptions import UserError
from datetime import datetime, timedelta, date
from lxml import etree
from lxml.etree import Element, SubElement
from odoo.tools.translate import _
from .bigint import BigInt

import logging
_logger = logging.getLogger(__name__)


import pytz
from six import string_types
import collections
try:
    from io import BytesIO
except:
    _logger.warning("no se ha cargado io")
try:
    from suds.client import Client
except:
    pass
try:
    import dicttoxml
except ImportError:
    _logger.warning('Cannot import dicttoxml library')

try:
    import pdf417gen
except ImportError:
    _logger.warning('Cannot import pdf417gen library')

try:
    import base64
except ImportError:
    _logger.warning('Cannot import base64 library')
try:
    from PIL import Image, ImageDraw, ImageFont
except:
    _logger.warning("no se ha cargado PIL")

# timbre patrón. Permite parsear y formar el
# ordered-dict patrón corespondiente al documento
timbre  = """<TED version="1.0"><DD><RE>99999999-9</RE><TD>11</TD><F>1</F>\
<FE>2000-01-01</FE><RR>99999999-9</RR><RSR>\
XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX</RSR><MNT>10000</MNT><IT1>IIIIIII\
</IT1><CAF version="1.0"><DA><RE>99999999-9</RE><RS>YYYYYYYYYYYYYYY</RS>\
<TD>10</TD><RNG><D>1</D><H>1000</H></RNG><FA>2000-01-01</FA><RSAPK><M>\
DJKFFDJKJKDJFKDJFKDJFKDJKDnbUNTAi2IaDdtAndm2p5udoqFiw==</M><E>Aw==</E></RSAPK>\
<IDK>300</IDK></DA><FRMA algoritmo="SHA1withRSA">\
J1u5/1VbPF6ASXkKoMOF0Bb9EYGVzQ1AMawDNOy0xSuAMpkyQe3yoGFthdKVK4JaypQ/F8\
afeqWjiRVMvV4+s4Q==</FRMA></CAF><TSTED>2014-04-24T12:02:20</TSTED></DD>\
<FRMT algoritmo="SHA1withRSA">jiuOQHXXcuwdpj8c510EZrCCw+pfTVGTT7obWm/\
fHlAa7j08Xff95Yb2zg31sJt6lMjSKdOK+PQp25clZuECig==</FRMT></TED>"""

try:
    import xmltodict
    result = xmltodict.parse(timbre)
except ImportError:
    _logger.warning('Cannot import xmltodict library')

server_url = {
    'SIICERT': 'https://maullin.sii.cl/DTEWS/',
    'SII': 'https://palena.sii.cl/DTEWS/',
}
claim_url = {
    'SIICERT': 'https://ws2.sii.cl/WSREGISTRORECLAMODTECERT/registroreclamodteservice',
    'SII': 'https://ws1.sii.cl/WSREGISTRORECLAMODTE/registroreclamodteservice',
}
TYPE2JOURNAL = {
    'out_invoice': 'sale',
    'in_invoice': 'purchase',
    'out_refund': 'sale',
    'in_refund': 'purchase',
}


class AccountInvoiceLine(models.Model):
    _inherit = 'account.invoice.line'

    sequence = fields.Integer(
            string="Sequence",
            default=-1,
        )

    @api.depends('price_unit', 'discount', 'invoice_line_tax_ids', 'quantity',
        'product_id', 'invoice_id.partner_id', 'invoice_id.currency_id',
        'invoice_id.company_id')
    def _compute_price(self):
        for line in self:
            currency = line.invoice_id and line.invoice_id.currency_id or None
            taxes = False
            total = 0
            if line.invoice_line_tax_ids:
                taxes = line.invoice_line_tax_ids.compute_all(line.price_unit, currency, line.quantity, product=line.product_id, partner=line.invoice_id.partner_id, discount=line.discount)
            if taxes:
                line.price_subtotal = price_subtotal_signed = taxes['total_excluded']
            else:
                total = line.currency_id.round((line.quantity * line.price_unit))
                total_discount = line.currency_id.round((total * ((line.discount or 0.0) / 100.0)))
                total -= total_discount
                line.price_subtotal = price_subtotal_signed = total
            if line.invoice_id.currency_id and line.invoice_id.currency_id != line.invoice_id.company_id.currency_id:
                price_subtotal_signed = line.invoice_id.currency_id.with_context(date=line.invoice_id._get_currency_rate_date()).compute(price_subtotal_signed, line.invoice_id.company_id.currency_id)
            sign = line.invoice_id.type in ['in_refund', 'out_refund'] and -1 or 1
            line.price_subtotal_signed = price_subtotal_signed * sign
            line.price_total = taxes['total_included'] if (taxes and taxes['total_included'] > total) else total


class Referencias(models.Model):
    _name = 'account.invoice.referencias'

    origen = fields.Char(
            string="Origin",
            )
    sii_referencia_TpoDocRef = fields.Many2one(
            'sii.document_class',
            string="SII Reference Document Type",
        )
    sii_referencia_CodRef = fields.Selection(
            [
                ('1', 'Anula Documento de Referencia'),
                ('2', 'Corrige texto Documento Referencia'),
                ('3', 'Corrige montos')
            ],
            string="SII Reference Code",
        )
    motivo = fields.Char(
            string="Motivo",
        )
    invoice_id = fields.Many2one(
            'account.invoice',
            ondelete='cascade',
            index=True,
            copy=False,
            string="Documento",
        )
    fecha_documento = fields.Date(
            string="Fecha Documento",
            required=True,
        )


class AccountInvoice(models.Model):
    _inherit = "account.invoice"

    def _default_journal_document_class_id(self, default=None):
        if self.document_class_id:
            return False
        ids = self._get_available_journal_document_class()
        document_classes = self.env['account.journal.sii_document_class'].browse(ids)
        if default:
            for dc in document_classes:
                if dc.sii_document_class_id.id == default:
                    self.journal_document_class_id = dc.id
                    self.document_class_id = dc.sii_document_class_id.id
        elif document_classes:
            default = self.get_document_class_default(document_classes)
        return default

    def _domain_journal_document_class_id(self):
        domain = self._get_available_journal_document_class()
        return [('id', 'in', domain)]

    @api.multi
    def get_barcode_img(self, columns=13, ratio=3):
        barcodefile = BytesIO()
        image = self.pdf417bc(self.sii_barcode, columns, ratio)
        image.save(barcodefile, 'PNG')
        data = barcodefile.getvalue()
        return base64.b64encode(data)

    def _get_barcode_img(self):
        for r in self:
            if r.sii_barcode:
                r.sii_barcode_img = r.get_barcode_img()

    vat_discriminated = fields.Boolean(
            'Discriminate VAT?',
            compute="get_vat_discriminated",
            store=True,
            readonly=False,
            help="Discriminate VAT on Quotations and Sale Orders?",
        )
    document_class_ids = fields.Many2many(
            'sii.document_class',
            related='journal_id.document_class_ids',
            string='Available Document Classes',
        )
    journal_document_class_id = fields.Many2one(
            'account.journal.sii_document_class',
            string='Documents Type',
            default=lambda self: self._default_journal_document_class_id(),
            readonly=True,
            states={'draft': [('readonly', False)]},
        )
    document_class_id = fields.Many2one(
            'sii.document_class',
            string='Document Type',
            readonly=True,
            states={'draft': [('readonly', False)]},
        )
    sii_code = fields.Integer(
            related='document_class_id.sii_code',
            string='Document Code',
            copy=False,
            readonly=True,
            store=True,
        )
    sii_document_number = BigInt(
            string='Document Number',
            copy=False,
            readonly=True,
            states={'draft': [('readonly', False)]},
        )
    responsability_id = fields.Many2one(
            'sii.responsability',
            string='Responsability',
            related='commercial_partner_id.responsability_id',
            store=True,
        )
    iva_uso_comun = fields.Boolean(
            string="Uso Común",
            readonly=True,
            states={'draft': [('readonly', False)]}
        ) # solamente para compras tratamiento del iva
    no_rec_code = fields.Selection(
            [
                    ('1', 'Compras destinadas a IVA a generar operaciones no gravados o exentas.'),
                    ('2', 'Facturas de proveedores registrados fuera de plazo.'),
                    ('3', 'Gastos rechazados.'),
                    ('4', 'Entregas gratuitas (premios, bonificaciones, etc.) recibidos.'),
                    ('9', 'Otros.')
            ],
            string="Código No recuperable",
            readonly=True,
            states={'draft': [('readonly', False)]},
        )# @TODO select 1 automático si es emisor 2Categoría
    use_documents = fields.Boolean(
            related='journal_id.use_documents',
            string='Use Documents?',
            readonly=True,
        )
    referencias = fields.One2many(
            'account.invoice.referencias',
            'invoice_id',
            readonly=True,
            states={'draft': [('readonly', False)]},
    )
    forma_pago = fields.Selection(
            [
                    ('1', 'Contado'),
                    ('2', 'Crédito'),
                    ('3', 'Gratuito')
            ],
            string="Forma de pago",
            readonly=True,
            states={'draft': [('readonly', False)]},
            default='1',
        )
    contact_id = fields.Many2one(
            'res.partner',
            string="Contacto",
        )
    sii_batch_number = fields.Integer(
            copy=False,
            string='Batch Number',
            readonly=True,
            help='Batch number for processing multiple invoices together',
        )
    sii_barcode = fields.Char(
            copy=False,
            string=_('SII Barcode'),
            help='SII Barcode Name',
            readonly=True,
            states={'draft': [('readonly', False)]},
        )
    sii_barcode_img = fields.Binary(
            string=_('SII Barcode Image'),
            help='SII Barcode Image in PDF417 format',
            compute="_get_barcode_img",
        )
    sii_message = fields.Text(
            string='SII Message',
            copy=False,
        )
    sii_xml_dte = fields.Text(
            string='SII XML DTE',
            copy=False,
            readonly=True,
            states={'draft': [('readonly', False)]},
        )
    sii_xml_request = fields.Many2one(
            'sii.xml.envio',
            string='SII XML Request',
            copy=False,
        )
    sii_result = fields.Selection(
            [
                ('draft', 'Borrador'),
                ('NoEnviado', 'No Enviado'),
                ('EnCola', 'En cola de envío'),
                ('Enviado', 'Enviado'),
                ('Aceptado', 'Aceptado'),
                ('Rechazado', 'Rechazado'),
                ('Reparo', 'Reparo'),
                ('Proceso', 'Procesado'),
                ('Anulado', 'Anulado'),
            ],
            string='Resultado',
            help="SII request result",
            copy=False,
        )
    canceled = fields.Boolean(
            string="Canceled?",
            copy=False,
        )
    estado_recep_dte = fields.Selection(
        [
            ('recibido', 'Recibido en DTE'),
            ('mercaderias', 'Recibido mercaderias'),
            ('validate', 'Validada Comercial')
        ],
        string="Estado de Recepcion del Envio",
        default='recibido',
        copy=False,
    )
    estado_recep_glosa = fields.Char(
        string="Información Adicional del Estado de Recepción",
        copy=False,
    )
    ticket = fields.Boolean(
            string="Formato Ticket",
            default=False,
            readonly=True,
            states={'draft': [('readonly', False)]},
    )
    claim = fields.Selection(
        [
            ('ACD', 'Acepta Contenido del Documento'),
            ('RCD', 'Reclamo al  Contenido del Documento '),
            ('ERM', ' Otorga  Recibo  de  Mercaderías  o Servicios'),
            ('RFP', 'Reclamo por Falta Parcial de Mercaderías'),
            ('RFT', 'Reclamo por Falta Total de Mercaderías'),
        ],
        string="Reclamo",
        copy=False,
    )
    claim_description = fields.Char(
        string="Detalle Reclamo",
        readonly=True,
    )
    purchase_to_done = fields.Many2many(
            'purchase.order',
            string="Ordenes de Compra a validar",
            domain=[('state', 'not in', ['done', 'cancel'])],
            readonly=True,
            states={'draft': [('readonly', False)]},
    )
    activity_description = fields.Many2one(
            'sii.activity.description',
            string="Giro",
            related="commercial_partner_id.activity_description",
            readonly=True,
    )
    amount_untaxed_global_discount = fields.Float(
            string="Global Discount Amount",
            store=True,
            default=0.00,
            compute='_compute_amount',
        )
    amount_untaxed_global_recargo = fields.Float(
            string="Global Recargo Amount",
            store=True,
            default=0.00,
            compute='_compute_amount',
        )
    global_descuentos_recargos = fields.One2many(
            'account.invoice.gdr',
            'invoice_id',
            string="Descuentos / Recargos globales",
            readonly=True,
            states={'draft': [('readonly', False)]},
        )
    acteco_ids = fields.Many2many(
        'partner.activities',
        related="commercial_partner_id.acteco_ids",
        string="Partner Activities"
    )
    acteco_id = fields.Many2one(
        'partner.activities',
        string="Partner Activity",
        readonly=True,
        states={'draft': [('readonly', False)]},
    )
    respuesta_ids = fields.Many2many(
        'sii.respuesta.cliente',
        string="Recepción del Cliente",
        readonly=True,
    )

    @api.onchange('invoice_line_ids')
    def _onchange_invoice_line_ids(self):
        i = 0
        for l in self.invoice_line_ids:
            i += 1
            if l.sequence == -1 or l.sequence == 0:
                l.sequence = i
        return super(AccountInvoice, self)._onchange_invoice_line_ids()

    @api.depends('state', 'journal_id', 'date_invoice', 'document_class_id')
    def _get_sequence_prefix(self):
        for invoice in self:
            if invoice.use_documents and invoice.type in ['out_invoice', 'out_refund']:
                invoice.sequence_number_next_prefix = invoice.document_class_id.doc_code_prefix or ''
            else:
                super(AccountInvoice, self)._get_sequence_prefix()

    @api.depends('state', 'journal_id', 'document_class_id')
    def _get_sequence_number_next(self):
        for invoice in self:
            if invoice.use_documents and invoice.type in ['out_invoice', 'out_refund']:
                invoice.sequence_number_next = invoice.journal_document_class_id.sequence_id.number_next_actual
            else:
                super(AccountInvoice, self)._get_sequence_number_next()

    @api.multi
    def compute_invoice_totals(self, company_currency, invoice_move_lines):
        '''
            @TODO Agregar Descuento Global como Concepto a parte en el caso de que sea asociado a una aplicación
        '''
        total = 0
        total_currency = 0
        amount_diff = self.amount_total
        amount_diff_currency = 0
        gdr = self.porcentaje_dr()
        if self.currency_id != company_currency:
            currency = self.currency_id.with_context(date=self.date_invoice or fields.Date.context_today(self))
            amount_diff = currency.compute(self.amount_total, company_currency)
            amount_diff_currency = self.amount_total
        for line in invoice_move_lines:
            #@TODO Posibilidad de GDR a exentos
            exento = False
            if line.get('tax_ids'):
                tax_ids = []
                # el ORM puede pasar (4, id, _) O (6, _, ids), asi que evaluar cada caso
                # ver el metodo write de models.py de odoo, para mayor informacion de cada caso
                if line.get('tax_ids')[0][0] == 4:
                    tax_ids = [line.get('tax_ids')[0][1]]
                elif line.get('tax_ids')[0][0] == 6:
                    tax_ids = line.get('tax_ids')[0][2]
                if tax_ids:
                    exento = self.env['account.tax'].search([('id', 'in', tax_ids), ('amount', '=', 0)])
            if not line.get('tax_line_id') and not exento:
                line['price'] *= gdr
            if line.get('amount_currency', False) and not line.get('tax_line_id'):
                if not exento:
                    line['amount_currency'] *= gdr
            if self.currency_id != company_currency:
                if not (line.get('currency_id') and line.get('amount_currency')):
                    line['currency_id'] = currency.id
                    line['amount_currency'] = currency.round(line['price'])
                    line['price'] = currency.compute(line['price'], company_currency)
            else:
                line['currency_id'] = False
                line['amount_currency'] = False
                line['price'] = self.currency_id.round(line['price'])
            ##para chequeo diferencia
            amount_diff -= line['price']
            if line.get('amount_currency', False):
                amount_diff_currency -= line['amount_currency']
            if self.type in ('out_invoice', 'in_refund'):
                total += line['price']
                total_currency += line['amount_currency'] or line['price']
                line['price'] = - line['price']
            else:
                total -= line['price']
                total_currency -= line['amount_currency'] or line['price']
        if amount_diff != 0:
            if self.type in ('out_invoice', 'in_refund'):
                invoice_move_lines[0]['price'] -= amount_diff
                total += amount_diff
            else:
                invoice_move_lines[0]['price'] += amount_diff
                total -= amount_diff
        if amount_diff_currency != 0:
            invoice_move_lines[0]['amount_currency'] += amount_diff_currency
            total_currency += amount_diff_currency
        return total, total_currency, invoice_move_lines

    #Se retomará en la próxima actualización para poder dar soporte a factura de compras
    #@api.multi
    #def finalize_invoice_move_lines(self, move_lines):
    #    if self.global_descuentos_recargos:
    #        move_lines = self._gd_move_lines(move_lines)
    #    taxes = self.tax_line_move_line_get()
    #    retencion = 0
    #    for t in taxes:
    #        if t['name'].find('RET - ', 0, 6) > -1:
    #            retencion += t['price']
    #    retencion = round(retencion)
    #    dif = 0
    #    total = self.amount_total
    #    for line in move_lines:
    #        if line[2]['name'] == '/' or line[2]['name'] == self.name:
    #            if line[2]['credit'] > 0:
    #                dif = total - line[2]['credit']
    #            else:
    #                dif = total - line[2]['debit']
    #    if dif != 0:
    #        move_lines = self._repairDiff( move_lines, dif)
    #    return move_lines

    @api.one
    @api.depends('invoice_line_ids.price_subtotal', 'tax_line_ids.amount', 'tax_line_ids.amount_rounding',
                 'currency_id', 'company_id', 'date_invoice', 'type', 'global_descuentos_recargos')
    def _compute_amount(self):
        neto = 0
        if self.global_descuentos_recargos:
            self.global_descuentos_recargos._untaxed_gdr()
            neto = self.global_descuentos_recargos.get_monto_aplicar()
            agrupados = self.global_descuentos_recargos.get_agrupados()
            self.amount_untaxed_global_discount = agrupados['D']
            self.amount_untaxed_global_recargo = agrupados['R']
        amount_tax = 0
        amount_retencion = 0
        included = False
        for tax in self.tax_line_ids:
            if tax.tax_id.price_include:
                included = True
            amount_tax += tax.amount
            amount_retencion += tax.amount_retencion
        self.amount_retencion = amount_retencion
        if included:
            neto += self.tax_line_ids._getNeto(self.currency_id)
            amount_retencion += amount_retencion
        else:
            neto += sum(line.price_subtotal for line in self.invoice_line_ids)
        self.amount_untaxed = neto
        self.amount_tax = amount_tax
        self.amount_total = self.amount_untaxed + self.amount_tax - amount_retencion
        amount_total_company_signed = self.amount_total
        amount_untaxed_signed = self.amount_untaxed
        if self.currency_id and self.currency_id != self.company_id.currency_id:
            currency_id = self.currency_id.with_context(date=self.date_invoice)
            amount_total_company_signed = currency_id.compute(self.amount_total, self.company_id.currency_id)
            amount_untaxed_signed = self.currency_id.compute(self.amount_untaxed, self.company_id.currency_id)
        sign = self.type in ['in_refund', 'out_refund'] and -1 or 1
        self.amount_total_company_signed = amount_total_company_signed * sign
        self.amount_total_signed = self.amount_total * sign
        self.amount_untaxed_signed = amount_untaxed_signed * sign

    def _prepare_tax_line_vals(self, line, tax):
        vals = super(AccountInvoice, self)._prepare_tax_line_vals(line, tax)
        vals['amount_retencion'] = tax['retencion']
        vals['retencion_account_id'] = self.type in ('out_invoice', 'in_invoice') and (tax['refund_account_id'] or line.account_id.id) or (tax['account_id'] or line.account_id.id)
        return vals

    @api.model
    def tax_line_move_line_get(self):
        res = []
        # keep track of taxes already processed
        done_taxes = []
        # loop the invoice.tax.line in reversal sequence
        for tax_line in sorted(self.tax_line_ids, key=lambda x: -x.sequence):
            amount = tax_line.amount + tax_line.amount_retencion
            if amount:
                tax = tax_line.tax_id
                if tax.amount_type == "group":
                    for child_tax in tax.children_tax_ids:
                        done_taxes.append(child_tax.id)
                done_taxes.append(tax.id)
                if tax_line.amount > 0:
                    res.append({
                        'invoice_tax_line_id': tax_line.id,
                        'tax_line_id': tax_line.tax_id.id,
                        'type': 'tax',
                        'name': tax_line.name,
                        'price_unit': tax_line.amount,
                        'quantity': 1,
                        'price': tax_line.amount,
                        'account_id': tax_line.account_id.id,
                        'account_analytic_id': tax_line.account_analytic_id.id,
                        'invoice_id': self.id,
                        'tax_ids': [(6, 0, done_taxes)] if tax_line.tax_id.include_base_amount else []
                    })
                if tax_line.amount_retencion > 0:
                    res.append({
                        'invoice_tax_line_id': tax_line.id,
                        'tax_line_id': tax_line.tax_id.id,
                        'type': 'tax',
                        'name': 'RET - ' + tax_line.name,
                        'price_unit': -tax_line.amount_retencion,
                        'quantity': 1,
                        'price': -tax_line.amount_retencion,
                        'account_id': tax_line.retencion_account_id.id,
                        'account_analytic_id': tax_line.account_analytic_id.id,
                        'invoice_id': self.id,
                        'tax_ids': [(6, 0, done_taxes)] if tax_line.tax_id.include_base_amount else []
                    })
        return res

    def porcentaje_dr(self):
        if not self.global_descuentos_recargos:
            return 1
        taxes = super(AccountInvoice, self).get_taxes_values()
        afecto = 0.00
        exento = 0.00
        porcentaje = 0.00
        total = 0.00
        for id, t in taxes.items():
            tax = self.env['account.tax'].browse(t['tax_id'])
            total += t['base']
            if tax.amount > 0:
                afecto += t['base']
            else:
                exento += t['base']
        agrupados = self.global_descuentos_recargos.get_agrupados()
        monto = agrupados['R'] - agrupados['D']
        if monto == 0:
            return 1
        porcentaje = (100.0 * monto) / afecto
        return 1 + (porcentaje / 100.0)

    def _get_grouped_taxes(self, line, taxes, tax_grouped={}):
        for tax in taxes:
            val = self._prepare_tax_line_vals(line, tax)
            # If the taxes generate moves on the same financial account as the invoice line,
            # propagate the analytic account from the invoice line to the tax line.
            # This is necessary in situations were (part of) the taxes cannot be reclaimed,
            # to ensure the tax move is allocated to the proper analytic account.
            if not val.get('account_analytic_id') and line.account_analytic_id and val['account_id'] == line.account_id.id:
                val['account_analytic_id'] = line.account_analytic_id.id
            key = self.env['account.tax'].browse(tax['id']).get_grouping_key(val)
            if key not in tax_grouped:
                tax_grouped[key] = val
            else:
                tax_grouped[key]['amount'] += val['amount']
                tax_grouped[key]['amount_retencion'] += val['amount_retencion']
                tax_grouped[key]['base'] += val['base']
        return tax_grouped

    @api.multi
    def get_taxes_values(self):
        tax_grouped = {}
        totales = {}
        included = False
        for line in self.invoice_line_ids:
            if (line.invoice_line_tax_ids and line.invoice_line_tax_ids[0].price_include) :# se asume todos losproductos vienen con precio incluido o no ( no hay mixes)
                if included or not tax_grouped:#genero error en caso de contenido mixto, en caso primer impusto no incluido segundo impuesto incluido
                    for t in line.invoice_line_tax_ids:
                        if t not in totales:
                            totales[t] = 0
                        amount_line = (self.currency_id.round(line.price_unit *line.quantity))
                        totales[t] += (amount_line * (1 - (line.discount / 100)))
                included = True
            else:
                included = False
            if (totales and not included) or (included and not totales):
                raise UserError('No se puede hacer timbrado mixto, todos los impuestos en este pedido deben ser uno de estos dos:  1.- precio incluído, 2.-  precio sin incluir')
            taxes = line.invoice_line_tax_ids.compute_all(line.price_unit, self.currency_id, line.quantity, line.product_id, self.partner_id, discount=line.discount)['taxes']
            tax_grouped = self._get_grouped_taxes(line, taxes, tax_grouped)
        #if totales:
        #    tax_grouped = {}
        #    for line in self.invoice_line_ids:
        #        for t in line.invoice_line_tax_ids:
        #            taxes = t.compute_all(totales[t], self.currency_id, 1)['taxes']
        #            tax_grouped = self._get_grouped_taxes(line, taxes, tax_grouped)
        #_logger.warning(tax_grouped)
        if not self.global_descuentos_recargos:
            return tax_grouped
        gdr = self.porcentaje_dr()
        taxes = {}
        for t, group in tax_grouped.items():
            if t not in taxes:
                taxes[t] = group
            tax = self.env['account.tax'].browse(group['tax_id'])
            if tax.amount > 0:
                taxes[t]['amount'] *= gdr
                taxes[t]['base'] *= gdr
        return taxes

    @api.onchange('global_descuentos_recargos')
    def _onchange_descuentos(self):
        self._onchange_invoice_line_ids()

    @api.onchange('payment_term_id', 'date_invoice')
    def _onchange_payment_term_date_invoice(self):
        super(AccountInvoice, self)._onchange_payment_term_date_invoice()
        if self.payment_term_id and self.payment_term_id.dte_sii_code:
            self.forma_pago = self.payment_term_id.dte_sii_code

    @api.model
    def _prepare_refund(self, invoice, date_invoice=None,
                        date=None, description=None, journal_id=None,
                        tipo_nota=61, mode='1'):
        values = super(AccountInvoice, self)._prepare_refund(invoice,
                                                             date_invoice,
                                                             date, description,
                                                             journal_id)
        jdc = self.env['account.journal.sii_document_class'].search(
                [
                    ('sii_document_class_id.sii_code', '=', tipo_nota),
                    ('journal_id', '=', invoice.journal_id.id),
                ],
                limit=1,
            )
        if invoice.type == 'out_invoice' and jdc.sii_document_class_id.document_type == 'credit_note':
            type = 'out_refund'
        elif invoice.type in ['out_refund', 'out_invoice']:
            type = 'out_invoice'
        elif invoice.type == 'in_invoice' and jdc.sii_document_class_id.document_type == 'credit_note':
            type = 'in_refund'
        elif invoice.type in ['in_refund', 'in_invoice']:
            type = 'in_invoice'
        values.update({
                'type': type,
                'document_class_id': jdc.sii_document_class_id.id,
                'journal_document_class_id': jdc.id,
                'referencias': [[0, 0, {
                        'origen':  invoice.sii_document_number,
                        'sii_referencia_TpoDocRef': invoice.document_class_id.id,
                        'sii_referencia_CodRef': mode,
                        'motivo': description,
                        'fecha_documento': invoice.date_invoice
                    }]],
            })
        return values

    @api.multi
    @api.returns('self')
    def refund(self, date_invoice=None, date=None, description=None,
               journal_id=None, tipo_nota=61, mode='1'):
        new_invoices = self.browse()
        for invoice in self:
            # create the new invoice
            values = self._prepare_refund(invoice, date_invoice=date_invoice,
                                          date=date, description=description,
                                          journal_id=journal_id,
                                          tipo_nota=tipo_nota, mode=mode)
            refund_invoice = self.create(values)
            invoice_type = {'out_invoice': ('customer invoices credit note'),
                            'out_refund': ('customer invoices debit note'),
                            'in_invoice': ('vendor bill credit note'),
                            'in_refund': ('vendor bill debit note')}
            message = _("This %s has been created from: <a href=# data-oe-model=account.invoice data-oe-id=%d>%s</a>") % (invoice_type[invoice.type], invoice.id, invoice.number)
            refund_invoice.message_post(body=message)
            new_invoices += refund_invoice
        return new_invoices

    def get_document_class_default(self, document_classes):
        document_class_id = None
        # @TODO compute from company or journal
        #if self.turn_issuer.vat_affected not in ['SI', 'ND']:
        #    exempt_ids = [
        #        self.env.ref('l10n_cl_fe.dc_y_f_dtn').id,
        #        self.env.ref('l10n_cl_fe.dc_y_f_dte').id]
        #    for document_class in document_classes:
        #        if document_class.sii_document_class_id.id in exempt_ids:
        #            document_class_id = document_class.id
        #            break
        #        else:
        #            document_class_id = document_classes.ids[0]
        #else:
        document_class_id = document_classes.ids[0]
        return document_class_id

    @api.multi
    def name_get(self):
        TYPES = {
            'out_invoice': _('Invoice'),
            'in_invoice': _('Supplier Invoice'),
            'out_refund': _('Refund'),
            'in_refund': _('Supplier Refund'),
        }
        result = []
        for inv in self:
            result.append(
                (inv.id, "%s %s" % (inv.number or TYPES[inv.type], inv.name or '')))
        return result

    @api.model
    def name_search(self, name, args=None, operator='ilike', limit=100):
        args = args or []
        recs = self.browse()
        if not recs:
            recs = self.search([('name', operator, name)] + args, limit=limit)
        return recs.name_get()

    def action_invoice_cancel(self):
        for r in self:
            if r.sii_xml_request and r.sii_result not in [False, 'draft',
                                                          'NoEnviado',
                                                          'Anulado']:
                raise UserError(
                            _('You can not cancel a valid document on SII'))
        return super(AccountInvoice, self).action_invoice_cancel()

    @api.multi
    def unlink(self):
        for r in self:
            if r.sii_xml_request and r.sii_result in ['Aceptado', 'Reparo',
                                                      'Rechazado']:
                raise UserError(
                            _('You can not delete a valid document on SII'))
        return super(AccountInvoice, self).unlink()

    def _buscarTaxEquivalente(self, tax):
        tax_n = self.env['account.tax'].search(
            [
                ('sii_code', '=', tax.sii_code),
                ('sii_type', '=', tax.sii_type),
                ('retencion', '=', tax.retencion),
                ('type_tax_use', '=', tax.type_tax_use),
                ('no_rec', '=', tax.no_rec),
                ('company_id', '=', self.company_id.id),
                ('price_include', '=', tax.price_include),
                ('amount', '=', tax.amount),
                ('amount_type', '=', tax.amount_type),
            ]
        )
        return tax_n

    def _crearTaxEquivalente(self, tax):
        tax_n = self.env['account.tax'].create({
            'sii_code': tax.sii_code,
            'sii_type': tax.sii_type,
            'retencion': tax.retencion,
            'type_tax_use': tax.type_tax_use,
            'no_rec': tax.no_rec,
            'name': tax.name,
            'description': tax.description,
            'tax_group_id': tax.tax_group_id.id,
            'company_id': self.company_id.id,
            'price_include': tax.price_include,
            'amount': tax.amount,
            'amount_type': tax.amount_type,
            'account_id': tax.account_id.id,
            'refund_account_id': tax.refund_account_id.id,
        })
        return tax_n

    @api.onchange('partner_id')
    def update_journal(self):
        self.journal_id = self._default_journal()
        self.set_default_journal()
        return self.update_domain_journal()

    @api.onchange('company_id')
    def _refreshRecords(self):
        self.journal_id = self._default_journal()
        journal = self.journal_id
        for line in self.invoice_line_ids:
            tax_ids = []
            if self._context.get('type') in ('out_invoice', 'in_refund'):
                line.account_id = journal.default_credit_account_id.id
            else:
                line.account_id = journal.default_debit_account_id.id
            if self._context.get('type') in ('out_invoice', 'out_refund'):
                for tax in line.product_id.taxes_id:
                    if tax.company_id.id == self.company_id.id:
                        tax_ids.append(tax.id)
                    else:
                        tax_n = self._buscarTaxEquivalente(tax)
                        if not tax_n:
                            tax_n = self._crearTaxEquivalente(tax)
                        tax_ids.append(tax_n.id)
                line.product_id.taxes_id = False
                line.product_id.taxes_id = tax_ids
            else:
                for tax in line.product_id.supplier_taxes_id:
                    if tax.company_id.id == self.company_id.id:
                        tax_ids.append(tax.id)
                    else:
                        tax_n = self._buscarTaxEquivalente(tax)
                        if not tax_n:
                            tax_n = self._crearTaxEquivalente(tax)
                        tax_ids.append(tax_n.id)
                line.invoice_line_tax_ids = False
                line.product_id.supplier_taxes_id.append = tax_ids
            line.invoice_line_tax_ids = False
            line.invoice_line_tax_ids = tax_ids

    def _get_available_journal_document_class(self):
        context = dict(self._context or {})
        journal_id = self.journal_id
        if not journal_id and 'default_journal_id' in context:
            journal_id = self.env['account.journal'].browse(context['default_journal_id'])
        if not journal_id:
            journal_id = self.env['account.journal'].search([('type','=','sale')],limit=1)
        invoice_type = self.type or context.get('default_type', False)
        if not invoice_type:
            invoice_type = 'in_invoice' if journal_id.type == 'purchase' else 'out_invoice'
        document_class_ids = []
        nd = False
        for ref in self.referencias:
            if not nd:
                nd = ref.sii_referencia_CodRef
        if invoice_type in ['out_invoice', 'in_invoice', 'out_refund', 'in_refund']:
            if journal_id:
                domain = [
                    ('journal_id', '=', journal_id.id),
                 ]
            else:
                operation_type = self.get_operation_type(invoice_type)
                domain = [
                    ('journal_id.type', '=', operation_type),
                 ]
            if invoice_type in ['in_refund', 'out_refund']:
                domain += [('sii_document_class_id.document_type', 'in', ['credit_note'])]
            else:
                options = ['invoice', 'invoice_in']
                if nd:
                    options.append('debit_note')
                domain += [('sii_document_class_id.document_type', 'in', options)]
            document_classes = self.env[
                'account.journal.sii_document_class'].search(domain)
            document_class_ids = document_classes.ids
        return document_class_ids

    @api.onchange('journal_document_class_id')
    def set_document_class_id(self):
        if self.move_id:
            return
        self.document_class_id = self.journal_document_class_id.sii_document_class_id.id

    @api.onchange('journal_id', 'partner_id')
    def update_domain_journal(self):
        document_classes = self._get_available_journal_document_class()
        result = {'domain': {
            'journal_document_class_id' : [('id', 'in', document_classes)],
        }}
        return result

    @api.depends('journal_id')
    @api.onchange('journal_id', 'partner_id')
    def set_default_journal(self, default=None):
        if not self.journal_document_class_id or self.journal_document_class_id.journal_id != self.journal_id:
            query = []
            if not default and not self.journal_document_class_id:
                query.append(
                    ('sii_document_class_id', '=', self.document_class_id.id),
                )
            if self.journal_document_class_id.journal_id != self.journal_id or not default:
                query.append(
                    ('journal_id', '=', self.journal_id.id)
                )
            if query:
                default = self.env['account.journal.sii_document_class'].search(
                    query,
                    order='sequence asc',
                    limit=1,
                ).id
            self.journal_document_class_id = self._default_journal_document_class_id(default)

    '''
    @TODO mejor forma de avisar problema conrut
    @api.onchange("document_class_id", "partner_id")
    def _check_vat(self):
        if self.partner_id and not self._es_boleta() and not self.partner_id.commercial_partner_id.document_number and self.vat_discriminated:
            raise UserError(_("The customer/supplier does not have a VAT \
defined. The type of invoicing document you selected requires you tu settle \
a VAT."))
    '''

    @api.depends(
        'document_class_id',
        'document_class_id.document_letter_id',
        'document_class_id.document_letter_id.vat_discriminated',
        'company_id',
        'company_id.invoice_vat_discrimination_default',)
    def get_vat_discriminated(self):
        for inv in self:
            vat_discriminated = False
            # agregarle una condicion: si el giro es afecto a iva, debe seleccionar factura, de lo contrario boleta (to-do)
            if inv.document_class_id.document_letter_id.vat_discriminated or inv.company_id.invoice_vat_discrimination_default == 'discriminate_default':
                vat_discriminated = True
            inv.vat_discriminated = vat_discriminated

    @api.one
    @api.constrains('reference',  'partner_id',  'company_id',  'type',  'journal_document_class_id')
    def _check_reference_in_invoice(self):
        if self.type in ['in_invoice', 'in_refund'] and self.sii_document_number:
            domain = [('type', '=', self.type),
                      ('reference', '=', self.reference),
                      ('partner_id', '=', self.partner_id.id),
                      ('journal_document_class_id.sii_document_class_id', '=',
                       self.document_class_id.id),
                      ('company_id', '=', self.company_id.id),
                      ('id', '!=', self.id),
                      ('state', '!=', 'cancel')]
            invoice_ids = self.search(domain)
            if invoice_ids:
                raise UserError(u'El numero de factura debe ser unico por Proveedor.\n'\
                                u'Ya existe otro documento con el numero: %s para el proveedor: %s' %
                                (self.sii_document_number, self.partner_id.display_name))

    @api.onchange('sii_document_number')
    def set_reference(self):
        if self.type in ['in_invoice', 'in_refund'] and self.sii_document_number:
            self.reference = "%s %s" %(
                        self.document_class_id.doc_code_prefix,
                         self.sii_document_number)

    @api.multi
    def action_move_create(self):
        for obj_inv in self:
            invtype = obj_inv.type
            if obj_inv.journal_document_class_id and not obj_inv.sii_document_number:
                if invtype in ('out_invoice', 'out_refund'):
                    if not obj_inv.journal_document_class_id.sequence_id:
                        raise UserError(_(
                            'Please define sequence on the journal related documents to this invoice.'))
                    to_write = {}
                    if not obj_inv.document_class_id:
                         to_write['document_class_id'] = obj_inv.journal_document_class_id.sii_document_class_id.id
                    sii_document_number = obj_inv.journal_document_class_id.sequence_id.next_by_id()
                    prefix = obj_inv.journal_document_class_id.sii_document_class_id.doc_code_prefix or ''
                    move_name = (prefix + str(sii_document_number)).replace(' ', '')
                    to_write.update({
                            'sii_document_number':  int(sii_document_number),
                            'move_name':  move_name
                        })
                    obj_inv.write(to_write)
        super(AccountInvoice, self).action_move_create()
        for obj_inv in self:
            invtype = obj_inv.type
            if invtype in ('in_invoice',  'in_refund') and  obj_inv.reference and not obj_inv.sii_document_number:
                obj_inv.sii_document_number = int(obj_inv.reference)
            document_class_id = obj_inv.document_class_id.id
            guardar = {'document_class_id':  document_class_id,
                       'sii_document_number':  obj_inv.sii_document_number,
                       'no_rec_code':  obj_inv.no_rec_code,
                       'iva_uso_comun':  obj_inv.iva_uso_comun,
                       }
            obj_inv.move_id.write(guardar)
        return True

    def get_operation_type(self, invoice_type):
        if invoice_type in ['in_invoice', 'in_refund']:
            operation_type = 'purchase'
        elif invoice_type in ['out_invoice', 'out_refund']:
            operation_type = 'sale'
        else:
            operation_type = False
        return operation_type

    def get_valid_document_letters(
            self,
            partner_id,
            operation_type='sale',
            company=False,
            vat_affected='SI',
            invoice_type='out_invoice',
            nd=False,):

        document_letter_obj = self.env['sii.document_letter']
        partner = self.partner_id

        if not partner_id or not company or not operation_type:
            return []

        partner = partner.commercial_partner_id
        if operation_type == 'sale':
            issuer_responsability_id = company.partner_id.responsability_id.id
            receptor_responsability_id = partner.responsability_id.id
            domain = [
                ('issuer_ids', '=', issuer_responsability_id),
                ('receptor_ids', '=', receptor_responsability_id),
                ]
            if invoice_type == 'out_invoice' and not nd:
                if vat_affected == 'SI':
                    domain.append(('name', '!=', 'C'))
                else:
                    domain.append(('name', '=', 'C'))
        elif operation_type == 'purchase':
            issuer_responsability_id = partner.responsability_id.id
            domain = [('issuer_ids', '=', issuer_responsability_id)]
        else:
            raise UserError(_('Operation Type Error'),
                             _('Operation Type Must be "Sale" or "Purchase"'))

        # TODO: fijar esto en el wizard, o llamar un wizard desde aca
        # if not company.partner_id.responsability_id.id:
        #     raise except_orm(_('You have not settled a tax payer type for your\
        #      company.'),
        #      _('Please, set your company tax payer type (in company or \
        #      partner before to continue.'))
        document_letter_ids = document_letter_obj.search(
             domain)
        return document_letter_ids

    @api.multi
    def _check_duplicate_supplier_reference(self):
        for invoice in self:
            if invoice.type in ('in_invoice',  'in_refund') and invoice.sii_document_number:
                if self.search(
                    [
                        ('sii_document_number', '=',  invoice.sii_document_number),
                        ('document_class_id', '=',  invoice.document_class_id.id),
                        ('partner_id', '=', invoice.partner_id.id),
                        ('type', '=', invoice.type),
                        ('id', '!=', invoice.id),
                     ]):
                    raise UserError('El documento %s, Folio %s de la Empresa %s ya se en cuentra registrado' % ( invoice.document_class_id.name,  invoice.sii_document_number,  invoice.partner_id.name))

    def _validaciones_uso_dte(self):
        ncs = [60, 61, 112, 802]
        nds = [55, 56, 111]
        if self.document_class_id.sii_code in ncs+nds and not self.referencias:
            raise UserError('Las Notas deben llevar por obligación una referencia al documento que están afectando')
        if not self.env.user.get_digital_signature(self.company_id):
            raise UserError(_('Usuario no autorizado a usar firma electrónica para esta compañia. Por favor solicatar autorización en la ficha de compañia del documento por alguien con los permisos suficientes de administrador'))
        if not self.env.ref('base.lang_es_CL').active:
            raise UserError(_('Lang es_CL must be enabled'))
        if not self.env.ref('base.CLP').active:
            raise UserError(_('Currency CLP must be enabled'))
        if self.type in ['out_refund', 'in_refund'] and \
            self.document_class_id.sii_code not in ncs:
            raise UserError(_('El tipo de documento %s, no es de tipo Rectificativo' % self.document_class_id.name) )
        if self.type in ['out_invoice', 'in_invoice'] and \
            self.document_class_id.sii_code in ncs:
            raise UserError(_('El tipo de documento %s, no es de tipo Documento' % self.document_class_id.name))
        for gd in self.global_descuentos_recargos:
            if gd.valor <= 0:
                raise UserError(_("No puede ir una línea igual o menor que 0, elimine la línea o verifique el valor ingresado"))
        if self.company_id.tax_calculation_rounding_method != 'round_globally':
            raise UserError("El método de redondeo debe ser Estríctamente Global")

    @api.multi
    def invoice_validate(self):
        for inv in self:
            if not inv.journal_id.use_documents or not inv.document_class_id.dte:
                continue
            inv._validaciones_uso_dte()
            inv.sii_result = 'NoEnviado'
            inv.responsable_envio = self.env.user.id
            if inv.type in ['out_invoice', 'out_refund']:
                if inv.journal_id.restore_mode:
                    inv.sii_result = 'Proceso'
                else:
                    inv._timbrar()
                    tiempo_pasivo = (datetime.now() + timedelta(hours=int(self.env['ir.config_parameter'].sudo().get_param('account.auto_send_dte', default=12))))
                    self.env['sii.cola_envio'].create({
                                    'company_id': inv.company_id.id,
                                    'doc_ids': [inv.id],
                                    'model': 'account.invoice',
                                    'user_id': self.env.uid,
                                    'tipo_trabajo': 'pasivo',
                                    'date_time': tiempo_pasivo,
                                    'send_email': False if inv.company_id.dte_service_provider=='SIICERT' or not self.env['ir.config_parameter'].sudo().get_param('account.auto_send_email', default=True) else True,
                                    })
            if inv.purchase_to_done:
                for ptd in inv.purchase_to_done:
                    ptd.write({'state': 'done'})
        return super(AccountInvoice, self).invoice_validate()

    @api.model
    def create(self, vals):
        inv = super(AccountInvoice, self).create(vals)
        inv.update_domain_journal()
        inv.set_default_journal()
        return inv

    @api.model
    def _default_journal(self):
        if self._context.get('default_journal_id', False):
            return self.env['account.journal'].browse(self._context.get('default_journal_id'))
        company_id = self._context.get('company_id', self.company_id or self.env.user.company_id)
        if self._context.get('honorarios', False):
            inv_type = self._context.get('type', 'out_invoice')
            inv_types = inv_type if isinstance(inv_type, list) else [inv_type]
            domain = [
                ('journal_document_class_ids.sii_document_class_id.document_letter_id.name','=','M'),
                ('type', 'in', [TYPE2JOURNAL[ty] for ty in inv_types if ty in TYPE2JOURNAL])
                ('company_id', '=', company_id.id),
            ]
            journal_id = self.env['account.journal'].search(domain, limit=1)
            return journal_id
        inv_type = self._context.get('type', 'out_invoice')
        inv_types = inv_type if isinstance(inv_type, list) else [inv_type]
        domain = [
            ('type', 'in', [TYPE2JOURNAL[ty] for ty in inv_types if ty in TYPE2JOURNAL]),
            ('company_id', '=', company_id.id),
        ]
        return self.env['account.journal'].search(domain, limit=1, order="sequence asc")

    def get_resolution_data(self, comp_id):
        resolution_data = {
            'dte_resolution_date': comp_id.dte_resolution_date,
            'dte_resolution_number': comp_id.dte_resolution_number}
        return resolution_data

    def create_template_envio(self, RutEmisor, RutReceptor, FchResol, NroResol,
                              TmstFirmaEnv, EnvioDTE, subject_serial_number,SubTotDTE):
        xml = '''<SetDTE ID="SetDoc">
<Caratula version="1.0">
<RutEmisor>{0}</RutEmisor>
<RutEnvia>{1}</RutEnvia>
<RutReceptor>{2}</RutReceptor>
<FchResol>{3}</FchResol>
<NroResol>{4}</NroResol>
<TmstFirmaEnv>{5}</TmstFirmaEnv>
{6}</Caratula>{7}
</SetDTE>
'''.format(RutEmisor, subject_serial_number, RutReceptor,
           FchResol, NroResol, TmstFirmaEnv, SubTotDTE, EnvioDTE)
        return xml

    def time_stamp(self, formato='%Y-%m-%dT%H:%M:%S'):
        tz = pytz.timezone('America/Santiago')
        return datetime.now(tz).strftime(formato)

    def create_template_doc(self, doc):
        xml = '''<DTE xmlns="http://www.sii.cl/SiiDte" version="1.0">
{}
</DTE>'''.format(doc)
        return xml

    def create_template_env(self, doc):
        xml = '''<?xml version="1.0" encoding="ISO-8859-1"?>
<EnvioDTE xmlns="http://www.sii.cl/SiiDte" \
xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" \
xsi:schemaLocation="http://www.sii.cl/SiiDte EnvioDTE_v10.xsd" \
version="1.0">
{}
</EnvioDTE>'''.format(doc)
        return xml

    def create_template_env_boleta(self, doc):
        xml = '''<?xml version="1.0" encoding="ISO-8859-1"?>
<EnvioBOLETA xmlns="http://www.sii.cl/SiiDte" \
xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" \
xsi:schemaLocation="http://www.sii.cl/SiiDte EnvioBOLETA_v11.xsd" \
version="1.0">
{}
</EnvioBOLETA>'''.format(doc)
        return xml

    def sign_full_xml(self, message, uri, type='doc'):
        user_id = self.env.user
        signature_id = user_id.get_digital_signature(self.company_id)
        if not signature_id:
            raise UserError(_('''There are not a Signature Cert Available for this user, please upload your signature or tell to someelse.'''))
        return signature_id.firmar(message, uri, type)

    def crear_intercambio(self):
        rut = self.format_vat(self.partner_id.commercial_partner_id.vat)
        envio = self._crear_envio(RUTRecep=rut)
        return envio['xml_envio'].encode('ISO-8859-1')

    def _create_attachment(self,):
        url_path = '/download/xml/invoice/%s' % (self.id)
        filename = ('%s.xml' % self.number).replace(' ', '_')
        att = self.env['ir.attachment'].search(
                [
                    ('name', '=', filename),
                    ('res_id', '=', self.id),
                    ('res_model', '=', 'account.invoice')
                ],
                limit=1,
            )
        if att:
            return att
        xml_intercambio = self.crear_intercambio()
        data = base64.b64encode(xml_intercambio)
        values = dict(
                        name=filename,
                        datas_fname=filename,
                        url=url_path,
                        res_model='account.invoice',
                        res_id=self.id,
                        type='binary',
                        datas=data,
                    )
        att = self.env['ir.attachment'].sudo().create(values)
        self.env['sii.respuesta.cliente'].create(
            {
                'exchange_id': att.id,
                'type': 'RecepcionEnvio',
                'recep_envio': 'no_revisado',
            }
        )
        return att

    @api.multi
    def action_invoice_sent(self):
        result = super(AccountInvoice, self).action_invoice_sent()
        if self.sii_xml_dte:
            att = self._create_attachment()
            result['context'].update({
                'default_attachment_ids': att.ids,
            })
        return result

    @api.multi
    def get_xml_file(self):
        url_path = '/download/xml/invoice/%s' % (self.id)
        return {
            'type' : 'ir.actions.act_url',
            'url': url_path,
            'target': 'self',
        }

    @api.multi
    def get_xml_exchange_file(self):
        url_path = '/download/xml/invoice_exchange/%s' % (self.id)
        return {
            'type': 'ir.actions.act_url',
            'url': url_path,
            'target': 'self',
        }

    def get_folio(self):
        # saca el folio directamente de la secuencia
        return self.sii_document_number

    def format_vat(self, value, con_cero=False):
        ''' Se Elimina el 0 para prevenir problemas con el sii, ya que las muestras no las toma si va con
        el 0 , y tambien internamente se generan problemas, se mantiene el 0 delante, para cosultas, o sino retorna "error de datos"'''
        if not value or value=='' or value == 0:
            value ="CL666666666"
            #@TODO opción de crear código de cliente en vez de rut genérico
        rut = value[:10] + '-' + value[10:]
        if not con_cero:
            rut = rut.replace('CL0','')
        rut = rut.replace('CL','')
        return rut

    def pdf417bc(self, ted, columns=13, ratio=3):
        bc = pdf417gen.encode(
            ted,
            security_level=5,
            columns=columns,
        )
        image = pdf417gen.render_image(
            bc,
            padding=15,
            scale=1,
            ratio=ratio,
        )
        return image

    @api.multi
    def get_related_invoices_data(self):
        """
        List related invoice information to fill CbtesAsoc.
        """
        self.ensure_one()
        rel_invoices = self.search([
            ('number', '=', self.origin),
            ('state', 'not in',
                ['draft', 'proforma', 'proforma2', 'cancel'])])
        return rel_invoices

    def _acortar_str(self, texto, size=1):
        c = 0
        cadena = ""
        while c < size and c < len(texto):
            cadena += texto[c]
            c += 1
        return cadena

    @api.multi
    def do_dte_send_invoice(self, n_atencion=None):
        ids = []
        envio_boleta = False
        for inv in self.with_context(lang='es_CL'):
            if inv.sii_result in ['', 'NoEnviado', 'Rechazado'] or inv.company_id.dte_service_provider == 'SIICERT':
                if inv.sii_result in ['Rechazado']:
                    inv._timbrar()
                    if len(inv.sii_xml_request) == 1:
                        inv.sii_xml_request.unlink()
                    else:
                        inv.sii_xml_request = False
                inv.sii_result = 'EnCola'
                ids.append(inv.id)
                if not envio_boleta and (inv._es_boleta() or inv._nc_boleta()):
                    envio_boleta = True
        if not isinstance(n_atencion, string_types):
            n_atencion = ''
        if ids:
            if envio_boleta:
                self.browse(ids).do_dte_send(n_atencion)
                return
            self.env['sii.cola_envio'].create({
                                'company_id': self[0].company_id.id,
                                'doc_ids': ids,
                                'model': 'account.invoice',
                                'user_id': self.env.user.id,
                                'tipo_trabajo': 'envio',
                                'n_atencion': n_atencion,
                                'send_email': False if self[0].company_id.dte_service_provider=='SIICERT' or not self.env['ir.config_parameter'].sudo().get_param('account.auto_send_email', default=True) else True,
                                })

    @api.multi
    def _es_boleta(self):
        return self.document_class_id.es_boleta()

    @api.multi
    def _nc_boleta(self):
        if not self.referencias or self.type != "out_refund":
            return False
        for r in self.referencias:
            return r.sii_referencia_TpoDocRef.es_nc_boleta()
        return False

    def _actecos_emisor(self):
        actecos = []
        if not self.journal_id.journal_activities_ids:
            raise UserError('El Diario no tiene ACTECOS asignados')
        for acteco in self.journal_id.journal_activities_ids:
            actecos.extend([{'Acteco': acteco.code}])
        return actecos

    def _id_doc(self, taxInclude=False, MntExe=0):
        IdDoc = collections.OrderedDict()
        IdDoc['TipoDTE'] = self.document_class_id.sii_code
        IdDoc['Folio'] = self.get_folio()
        IdDoc['FchEmis'] = self.date_invoice
        if self._es_boleta():
            IdDoc['IndServicio'] = 3 #@TODO agregar las otras opciones a la fichade producto servicio
        if self.ticket and not self._es_boleta():
            IdDoc['TpoImpresion'] = "T"
        #if self.tipo_servicio:
        #    Encabezado['IdDoc']['IndServicio'] = 1,2,3,4
        # todo: forma de pago y fecha de vencimiento - opcional
        if taxInclude and MntExe == 0 and not self._es_boleta():
            IdDoc['MntBruto'] = 1
        if not self._es_boleta():
            IdDoc['FmaPago'] = self.forma_pago or 1
        if not taxInclude and self._es_boleta():
            IdDoc['IndMntNeto'] = 2
        #if self._es_boleta():
            #Servicios periódicos
        #    IdDoc['PeriodoDesde'] =
        #    IdDoc['PeriodoHasta'] =
        if not self._es_boleta():
            IdDoc['FchVenc'] = self.date_due or datetime.strftime(datetime.now(), '%Y-%m-%d')
        return IdDoc

    def _emisor(self):
        Emisor= collections.OrderedDict()
        Emisor['RUTEmisor'] = self.format_vat(self.company_id.vat)
        if self._es_boleta():
            Emisor['RznSocEmisor'] = self._acortar_str(self.company_id.partner_id.name, 100)
            Emisor['GiroEmisor'] = self._acortar_str(self.company_id.activity_description.name, 80)
        else:
            Emisor['RznSoc'] = self._acortar_str(self.company_id.partner_id.name, 100)
            Emisor['GiroEmis'] = self._acortar_str(self.company_id.activity_description.name, 80)
            if self.company_id.phone:
                Emisor['Telefono'] = self._acortar_str(self.company_id.phone, 20)
            Emisor['CorreoEmisor'] = self.company_id.dte_email_id.name_get()[0][1]
            Emisor['item'] = self._actecos_emisor()
        if self.journal_id.sucursal_id:
            Emisor['Sucursal'] = self._acortar_str(self.journal_id.sucursal_id.name, 20)
            Emisor['CdgSIISucur'] = self._acortar_str(self.journal_id.sucursal_id.sii_code, 9)
        Emisor['DirOrigen'] = self._acortar_str(self.company_id.street + ' ' + (self.company_id.street2 or ''), 70)
        if not self.company_id.city_id:
            raise UserError("Debe ingresar la Comuna de compañía emisora")
        Emisor['CmnaOrigen'] = self.company_id.city_id.name
        if not self.company_id.city:
            raise UserError("Debe ingresar la Ciudad de compañía emisora")
        Emisor['CiudadOrigen'] = self.company_id.city
        return Emisor

    def _receptor(self):
        Receptor = collections.OrderedDict()
        if not self.commercial_partner_id.vat and not self._es_boleta() and not self._nc_boleta():
            raise UserError("Debe Ingresar RUT Receptor")
        #if self._es_boleta():
        #    Receptor['CdgIntRecep']
        Receptor['RUTRecep'] = self.format_vat(self.commercial_partner_id.vat)
        Receptor['RznSocRecep'] = self._acortar_str( self.commercial_partner_id.name, 100)
        if not self.partner_id or Receptor['RUTRecep'] == '66666666-6':
            return Receptor
        if not self._es_boleta() and not self._nc_boleta():
            GiroRecep = self.acteco_id.name or self.commercial_partner_id.activity_description.name
            if not GiroRecep:
                raise UserError(_('Seleccione giro del partner'))
            Receptor['GiroRecep'] = self._acortar_str(GiroRecep, 40)
        if self.partner_id.phone or self.commercial_partner_id.phone:
            Receptor['Contacto'] = self._acortar_str(self.partner_id.phone or self.commercial_partner_id.phone or self.partner_id.email, 80)
        if (self.commercial_partner_id.email or self.commercial_partner_id.dte_email or self.partner_id.email or self.partner_id.dte_email) and not self._es_boleta():
            Receptor['CorreoRecep'] = self.commercial_partner_id.dte_email or self.partner_id.dte_email or self.commercial_partner_id.email or self.partner_id.email
        street_recep = (self.partner_id.street or self.commercial_partner_id.street or False)
        if not street_recep and not self._es_boleta() and not self._nc_boleta():
        # or self.indicador_servicio in [1, 2]:
            raise UserError('Debe Ingresar dirección del cliente')
        street2_recep = (self.partner_id.street2 or self.commercial_partner_id.street2 or False)
        if street_recep or street2_recep:
            Receptor['DirRecep'] = self._acortar_str(street_recep + (' ' + street2_recep if street2_recep else ''), 70)
        cmna_recep = self.partner_id.city_id.name or self.commercial_partner_id.city_id.name
        if not cmna_recep and not self._es_boleta() and not self._nc_boleta():
            raise UserError('Debe Ingresar Comuna del cliente')
        else:
            Receptor['CmnaRecep'] = cmna_recep
        ciudad_recep = self.partner_id.city or self.commercial_partner_id.city
        if ciudad_recep:
            Receptor['CiudadRecep'] = ciudad_recep
        return Receptor

    def _totales_otra_moneda(self, currency_id, MntExe, MntNeto, IVA, TasaIVA,
                             ImptoReten, MntTotal=0, MntBase=0):
        Totales = collections.OrderedDict()
        Totales['TpoMoneda'] = self._acortar_str(currency_id.abreviatura, 15)
        Totales['TpoCambio'] = currency_id.rate
        if MntNeto > 0:
            if currency_id != self.currency_id:
                MntNeto = currency_id.compute(MntNeto, self.currency_id)
            Totales['MntNetoOtrMnda'] = MntNeto
        if MntExe:
            if currency_id != self.currency_id:
                MntExe = currency_id.compute(MntExe, self.currency_id)
            Totales['MntExeOtrMnda'] = MntExe
        if MntBase and MntBase > 0:
            Totales['MntFaeCarneOtrMnda'] = MntBase
        if TasaIVA:
            if currency_id != self.currency_id:
                IVA = currency_id.compute(IVA, self.currency_id)
            Totales['IVAOtrMnda'] = IVA
        if ImptoReten:
            for item in ImptoReten:
                ret = {'ImptRetOtrMnda': collections.OrderedDict()}
                ret['ImptRetOtrMnda']['TipoImpOtrMnda'] = item['ImptRet']['TipoImp']
                ret['ImptRetOtrMnda']['TasaImpOtrMnda'] = item['ImptRet']['TasaImp']
                if currency_id != self.currency_id:
                    ret['ImptRetOtrMnda']['MontoImp'] = currency_id.compute(item['ImptRet']['MontoImp'], self.currency_id)
                ret['ImptRetOtrMnda']['ValorImpOtrMnda'] = item['ImptRet']['MontoImp']
            Totales['item_ret_otr'] = ret
        if currency_id != self.currency_id:
            MntTotal = currency_id.compute(MntTotal, self.currency_id)
        Totales['MntTotOtrMnda'] = MntTotal
        #Totales['MontoNF']
        #Totales['TotalPeriodo']
        #Totales['SaldoAnterior']
        #Totales['VlrPagar']
        return Totales

    def _totales_normal(self, currency_id, MntExe, MntNeto, IVA, TasaIVA,
                        ImptoReten, MntTotal=0, MntBase=0):
        Totales = collections.OrderedDict()
        if MntNeto > 0:
            if currency_id != self.currency_id:
                    MntNeto = currency_id.compute(MntNeto, self.currency_id)
            Totales['MntNeto'] = currency_id.round(MntNeto)
        if MntExe:
            if currency_id != self.currency_id:
                MntExe = currency_id.compute(MntExe, self.currency_id)
            Totales['MntExe'] = currency_id.round(MntExe)
        if MntBase > 0:
            Totales['MntBase'] = currency_id.round(MntBase)
        if TasaIVA:
            Totales['TasaIVA'] = TasaIVA
            if currency_id != self.currency_id:
                IVA = currency_id.compute(IVA, self.currency_id)
            Totales['IVA'] = currency_id.round(IVA)
        if ImptoReten:
            '''@TODO desglose ImptoReten'''
            Totales['item_ret'] = ImptoReten
        if currency_id != self.currency_id:
            MntTotal = currency_id.compute(MntTotal, self.currency_id)
        Totales['MntTotal'] = currency_id.round(MntTotal)
        #Totales['MontoNF']
        #Totales['TotalPeriodo']
        #Totales['SaldoAnterior']
        #Totales['VlrPagar']
        return Totales

    def _es_exento(self):
        return self.document_class_id.sii_code in [34, 41] or (self.referencias and self.referencias[0].sii_referencia_TpoDocRef.sii_code in [ 34, 41])

    def _totales(self, MntExe=0, no_product=False, taxInclude=False):
        MntNeto = 0
        IVA = False
        ImptoReten = False
        TasaIVA = False
        MntIVA = 0
        MntBase = 0
        OtrosImp = []
        if self._es_exento():
            MntExe = self.amount_total
            if no_product:
                MntExe = 0
            if self.amount_tax > 0:
                raise UserError("NO pueden ir productos afectos en documentos exentos")
        elif self.amount_untaxed and self.amount_untaxed != 0:
            if not self._es_boleta() or not taxInclude:
                IVA = False
                for t in self.tax_line_ids:
                    if t.tax_id.sii_code in [14, 15]:
                        IVA = t
                    elif t.tax_id.sii_code in [15, 17, 18, 19, 27, 271, 26]:
                        OtrosImp.append(t)
                    if t.tax_id.sii_code in [14, 15]:
                        MntNeto += t.base
                    if t.tax_id.sii_code in [17]:
                        MntBase += IVA.base # @TODO Buscar forma de calcular la base para faenamiento
        if self.amount_tax == 0 and MntExe > 0 and not self._es_exento():
            raise UserError("Debe ir almenos un producto afecto")
        if MntExe > 0:
            MntExe = MntExe
        if not self._es_boleta() or not taxInclude:
            if IVA:
                if not self._es_boleta():
                    TasaIVA = round(IVA.tax_id.amount, 2)
                MntIVA = IVA.amount
            if no_product:
                MntNeto = 0
                if not self._es_boleta():
                    TasaIVA = 0
                MntIVA = 0
        if OtrosImp:
            ImptoReten = []
            for item in OtrosImp:
                itemRet = {'ImptoReten': collections.OrderedDict()}
                itemRet['ImptoReten']['TipoImp'] = item.tax_id.sii_code
                itemRet['ImptoReten']['TasaImp'] = item.tax_id.amount
                itemRet['ImptoReten']['MontoImp'] = item.amount_retencion if item.tax_id.sii_type == 'R' else item.amount
                ImptoReten.append(itemRet)
        MntTotal = self.amount_total
        if no_product:
            MntTotal = 0
        return MntExe, MntNeto, MntIVA, TasaIVA, ImptoReten, MntTotal, MntBase

    def _encabezado(self, MntExe=0, no_product=False, taxInclude=False):
        Encabezado = collections.OrderedDict()
        Encabezado['IdDoc'] = self._id_doc(taxInclude, MntExe)
        Encabezado['Emisor'] = self._emisor()
        Encabezado['Receptor'] = self._receptor()
        currency_base = self.env.ref('base.CLP').with_context(date=self.date_invoice)
        another_currency_id = False
        if self.currency_id != currency_base:
            another_currency_id = self.currency_id
        MntExe, MntNeto, IVA, TasaIVA, ImptoReten, MntTotal, MntBase = self._totales(MntExe, no_product, taxInclude)
        Encabezado['Totales'] = self._totales_normal(currency_base, MntExe,
                                                     MntNeto, IVA, TasaIVA,
                                                     ImptoReten, MntTotal,
                                                     MntBase)
        if another_currency_id:
            Encabezado['OtraMoneda'] = self._totales_otra_moneda(
                            another_currency_id, MntExe, MntNeto, IVA, TasaIVA,
                            ImptoReten, MntTotal, MntBase)
        return Encabezado

    def _validaciones_caf(self, caf):
        if not self.commercial_partner_id.vat and not self._es_boleta() and not self._nc_boleta():
            raise UserError(_("Fill Partner VAT"))
        timestamp = self.time_stamp()
        invoice_date = date(int(self.date_invoice[:4]), int(self.date_invoice[5:7]), int(self.date_invoice[8:10]))
        fecha_timbre = date(int(timestamp[:4]), int(timestamp[5:7]), int(timestamp[8:10]))
        if fecha_timbre < invoice_date:
            raise UserError("La fecha de timbraje no puede ser menor a la fecha de emisión del documento")
        if fecha_timbre < \
            date(int(caf['FA'][:4]), int(caf['FA'][5:7]), int(caf['FA'][8:10])):
            raise UserError("La fecha del timbraje no puede ser menor a la fecha de emisión del CAF")
        return timestamp

    @api.multi
    def get_barcode(self, no_product=False):
        ted = False
        folio = self.get_folio()
        result['TED']['DD']['RE'] = self.format_vat(self.company_id.vat)
        result['TED']['DD']['TD'] = self.document_class_id.sii_code
        result['TED']['DD']['F'] = folio
        result['TED']['DD']['FE'] = self.date_invoice
        result['TED']['DD']['RR'] = self.format_vat(self.commercial_partner_id.vat)
        result['TED']['DD']['RSR'] = self._acortar_str(self.commercial_partner_id.name,40)
        result['TED']['DD']['MNT'] = self.currency_id.round(self.amount_total)
        if no_product:
            result['TED']['DD']['MNT'] = 0
        for line in self.invoice_line_ids:
            result['TED']['DD']['IT1'] = self._acortar_str(
                            line.product_id.name,40)
            if line.product_id.default_code:
                result['TED']['DD']['IT1'] = self._acortar_str(
                                    line.product_id.name.replace('['+line.product_id.default_code+'] ',''),40)
            break
        resultcaf = self.journal_document_class_id.sequence_id.get_caf_file(
                    self.get_folio())
        result['TED']['DD']['CAF'] = resultcaf['AUTORIZACION']['CAF']
        dte = result['TED']['DD']
        timestamp = self._validaciones_caf(
                    resultcaf['AUTORIZACION']['CAF']['DA'])
        dte['TSTED'] = timestamp
        dicttoxml.set_debug(False)
        ddxml = '<DD>'+dicttoxml.dicttoxml(
            dte, root=False, attr_type=False).decode().replace(
            '<key name="@version">1.0</key>', '', 1).replace(
            '><key name="@version">1.0</key>', ' version="1.0">', 1).replace(
            '><key name="@algoritmo">SHA1withRSA</key>',
            ' algoritmo="SHA1withRSA">').replace(
            '<key name="#text">', '').replace(
            '</key>', '').replace('<CAF>', '<CAF version="1.0">') + '</DD>'
        keypriv = resultcaf['AUTORIZACION']['RSASK'].replace('\t', '')
        root = etree.XML( ddxml )
        ddxml = etree.tostring(root)
        signature_id = self.env.user.get_digital_signature(self.company_id)
        if not signature_id:
            raise UserError(_('''There are not a Signature Cert Available for this user, please upload your signature or tell to someelse.'''))
        frmt = signature_id.generar_firma(ddxml, privkey=keypriv)
        ted = (
            '''<TED version="1.0">{}<FRMT algoritmo="SHA1withRSA">{}\
</FRMT></TED>''').format(ddxml.decode(), frmt)
        self.sii_barcode = ted
        ted += '<TmstFirma>{}</TmstFirma>'.format(timestamp)
        return ted

    @api.multi
    def is_price_included(self):
        if not self.invoice_line_ids or not self.invoice_line_ids[0].invoice_line_tax_ids:
            return False
        tax = self.invoice_line_ids[0].invoice_line_tax_ids[0]
        if tax.price_include or (not tax.sii_detailed and (self._es_boleta() or self._nc_boleta()) ):
            return True
        return False

    def _invoice_lines(self):
        invoice_lines = []
        no_product = False
        MntExe = 0
        currency_base = self.env.ref('base.CLP').with_context(
                    date=self.date_invoice)
        currency_id = False
        if self.currency_id != currency_base:
            currency_id = self.currency_id.with_context(date=self.date_invoice)
        if self.env['account.invoice.line'].search([
                '|',
                ('sequence', '=', -1),
                ('sequence', '=', 0),
                ('invoice_id', '=', self.id)
            ]):
            self._onchange_invoice_line_ids()
        for line in self.invoice_line_ids:
            if line.product_id.default_code == 'NO_PRODUCT':
                no_product = True
            lines = collections.OrderedDict()
            lines['NroLinDet'] = line.sequence
            if line.product_id.default_code and not no_product:
                lines['CdgItem'] = collections.OrderedDict()
                lines['CdgItem']['TpoCodigo'] = 'INT1'
                lines['CdgItem']['VlrCodigo'] = line.product_id.default_code
            taxInclude = False
            for t in line.invoice_line_tax_ids:
                taxInclude = t.price_include or ( (self._es_boleta() or self._nc_boleta()) and not t.sii_detailed )
                if t.amount == 0 or t.sii_code in [0]:#@TODO mejor manera de identificar exento de afecto
                    lines['IndExe'] = 1
                    MntExe += currency_base.round(line.price_subtotal)
            #if line.product_id.type == 'events':
            #   lines['ItemEspectaculo'] =
#            if self._es_boleta():
#                lines['RUTMandante']
            lines['NmbItem'] = self._acortar_str(line.product_id.name, 80) #
            lines['DscItem'] = self._acortar_str(line.name, 1000) #descripción más extenza
            if line.product_id.default_code:
                lines['NmbItem'] = self._acortar_str(
                                    line.product_id.name.replace(
                                    '[%s] ' % line.product_id.default_code,
                                    ''), 80)
            #lines['InfoTicket']
            qty = round(line.quantity, 4)
            if not no_product:
                lines['QtyItem'] = qty
            if qty == 0 and not no_product:
                lines['QtyItem'] = 1
            elif qty < 0:
                raise UserError("NO puede ser menor que 0")
            if not no_product:
                lines['UnmdItem'] = line.uom_id.name[:4]
                lines['PrcItem'] = round(line.price_unit, 6)
                for t in line.invoice_line_tax_ids:
                    if t.sii_code in [26, 27, 271]:#@Agregar todos los adicionales
                        lines['CodImpAdic'] = t.sii_code
                if currency_id:
                    lines['OtrMnda'] = collections.OrderedDict()
                    lines['OtrMnda']['PrcOtrMon'] = round(currency_base.compute( line.price_unit, currency_id, round=False), 6)
                    lines['OtrMnda']['Moneda'] = self._acortar_str(currency_id.name, 3)
                    lines['OtrMnda']['FctConv'] = round(currency_id.rate, 4)
            if line.discount > 0:
                lines['DescuentoPct'] = line.discount
                DescMonto = (((line.discount / 100) * lines['PrcItem'])* qty)
                lines['DescuentoMonto'] = currency_base.round(DescMonto)
                if currency_id:
                    lines['DescuentoMonto'] = currency_base.compute(DescMonto, currency_id)
                    lines['OtrMnda']['DctoOtrMnda'] = DescMonto
            if not no_product and not taxInclude:
                price_subtotal = line.price_subtotal
                if currency_id:
                    lines['OtrMnda']['MontoItemOtrMnda'] = price_subtotal
                    price_subtotal = currency_base.compute(price_subtotal, currency_id)
                lines['MontoItem'] = currency_base.round(price_subtotal)
            elif not no_product:
                price_total = line.price_total
                if currency_id:
                    lines['OtrMnda']['MontoItemOtrMnda'] = price_total
                    price_total = currency_base.compute(price_total, currency_id)
                lines['MontoItem'] = currency_base.round(price_total)
            if no_product:
                lines['MontoItem'] = 0
            if lines['MontoItem'] < 0:
                raise UserError(_('No pueden ir valores negativos en las líneas de detalle'))
            if lines.get('PrcItem', 1) == 0:
                del(lines['PrcItem'])
            invoice_lines.extend([{'Detalle': lines}])
            if 'IndExe' in lines:
                taxInclude = False
        return {
                'invoice_lines': invoice_lines,
                'MntExe': MntExe,
                'no_product': no_product,
                'tax_include': taxInclude,
                }

    def _gdr(self):
        result = []
        lin_dr = 1
        for dr in self.global_descuentos_recargos:
            dr_line = collections.OrderedDict()
            dr_line['NroLinDR'] = lin_dr
            dr_line['TpoMov'] = dr.type
            if dr.gdr_detail:
                dr_line['GlosaDR'] = dr.gdr_detail
            disc_type = "%"
            if dr.gdr_type == "amount":
                disc_type = "$"
            dr_line['TpoValor'] = disc_type
            currency_base = self.env.ref('base.CLP').with_context(
                                                        date=self.date_invoice)
            dr_line['ValorDR'] = currency_base.round(dr.valor)
            if self.currency_id != currency_base:
                currency_id = self.currency_id
                dr_line['ValorDROtrMnda'] = currency_base.compute(dr.valor, currency_id)
            if self.document_class_id.sii_code in [34] and (self.referencias and self.referencias[0].sii_referencia_TpoDocRef.sii_code == '34'):#solamente si es exento
                dr_line['IndExeDR'] = 1
            dr_lines = [{'DscRcgGlobal': dr_line}]
            result.append(dr_lines)
            lin_dr += 1
        return result

    def _dte(self, n_atencion=None):
        dte = collections.OrderedDict()
        invoice_lines = self._invoice_lines()
        dte['Encabezado'] = self._encabezado(
            invoice_lines['MntExe'],
            invoice_lines['no_product'],
            invoice_lines['tax_include']
        )
        lin_ref = 1
        ref_lines = []
        if self.company_id.dte_service_provider == 'SIICERT' and isinstance(n_atencion, string_types) and n_atencion != '' and not self._es_boleta():
            ref_line = collections.OrderedDict()
            ref_line['NroLinRef'] = lin_ref
            ref_line['TpoDocRef'] = "SET"
            ref_line['FolioRef'] = self.get_folio()
            ref_line['FchRef'] = datetime.strftime(datetime.now(), '%Y-%m-%d')
            ref_line['RazonRef'] = "CASO "+n_atencion+"-" + str(self.sii_batch_number)
            lin_ref = 2
            ref_lines.extend([{'Referencia':ref_line}])
        if self.referencias:
            for ref in self.referencias:
                ref_line = collections.OrderedDict()
                ref_line['NroLinRef'] = lin_ref
                if not self._es_boleta():
                    if  ref.sii_referencia_TpoDocRef:
                        ref_line['TpoDocRef'] = self._acortar_str(ref.sii_referencia_TpoDocRef.doc_code_prefix, 3) if ref.sii_referencia_TpoDocRef.use_prefix else ref.sii_referencia_TpoDocRef.sii_code
                        ref_line['FolioRef'] = ref.origen
                    ref_line['FchRef'] = ref.fecha_documento or datetime.strftime(datetime.now(), '%Y-%m-%d')
                if ref.sii_referencia_CodRef not in ['','none', False]:
                    ref_line['CodRef'] = ref.sii_referencia_CodRef
                ref_line['RazonRef'] = ref.motivo
                if self._es_boleta():
                    ref_line['CodVndor'] = self.seler_id.id
                    ref_lines['CodCaja'] = self.journal_id.point_of_sale_id.name
                ref_lines.extend([{'Referencia': ref_line}])
                lin_ref += 1
        dte['item'] = invoice_lines['invoice_lines']
        if self.global_descuentos_recargos:
            dte['drlines'] = self._gdr()
        dte['reflines'] = ref_lines
        dte['TEDd'] = self.get_barcode(invoice_lines['no_product'])
        return dte

    def _dte_to_xml(self, dte, tpo_dte="Documento"):
        ted = dte[tpo_dte + ' ID']['TEDd']
        dte[(tpo_dte + ' ID')]['TEDd'] = ''
        xml = dicttoxml.dicttoxml(
            dte, root=False, attr_type=False).decode() \
            .replace('<item >', '').replace('<item>', '').replace('</item>', '')\
            .replace('<reflines>', '').replace('</reflines>', '')\
            .replace('<TEDd>', '').replace('</TEDd>', '')\
            .replace('</'+ tpo_dte + '_ID>','\n'+ted+'\n</'+ tpo_dte + '_ID>')\
            .replace('<drlines>', '').replace('</drlines>', '')\
            .replace('<item_ret>', '').replace('</item_ret>', '')\
            .replace('<item_ret_otr>', '').replace('</item_ret_otr>', '')
        return xml

    def _tpo_dte(self):
        tpo_dte = "Documento"
        if self.document_class_id.sii_code == 43:
            tpo_dte = 'Liquidacion'
        return tpo_dte

    def _timbrar(self, n_atencion=None):
        folio = self.get_folio()
        tpo_dte = self._tpo_dte()
        doc_id_number = "F{}T{}".format(folio, self.document_class_id.sii_code)
        doc_id = '<' + tpo_dte + ' ID="{}">'.format(doc_id_number)
        dte = collections.OrderedDict()
        dte[(tpo_dte + ' ID')] = self._dte(n_atencion)
        xml = self._dte_to_xml(dte, tpo_dte)
        root = etree.XML(xml)
        xml_pret = etree.tostring(
                root,
                pretty_print=True
            ).decode().replace(
                    '<' + tpo_dte + '_ID>',
                    doc_id
                ).replace('</' + tpo_dte + '_ID>', '</' + tpo_dte + '>')
        envelope_efact = self.create_template_doc(xml_pret)
        type = 'doc'
        if self._es_boleta():
            type = 'bol'
        einvoice = self.sign_full_xml(
                envelope_efact,
                doc_id_number,
                type,
            )
        self.sii_xml_dte = einvoice

    def _crear_envio(self, n_atencion=None, RUTRecep="60803000-K"):
        dicttoxml.set_debug(False)
        clases = {}
        company_id = False
        es_boleta = False
        batch = 0
        for inv in self.with_context(lang='es_CL'):
            if not inv.sii_batch_number or inv.sii_batch_number == 0:
                batch += 1
                inv.sii_batch_number = batch #si viene una guía/nota regferenciando una factura, que por numeración viene a continuación de la guia/nota, será recahazada laguía porque debe estar declarada la factura primero
            if inv.sii_batch_number != 0 and inv._es_boleta():
                if not es_boleta and clases:
                    raise UserError('No se puede hacer envío masivo con contenido mixto, para este envío solamente boleta electrónica, boleta exenta electrónica o NC de Boleta ( o eliminar los casos descitos del set)')
                es_boleta = True
            if inv.company_id.dte_service_provider == 'SIICERT' or inv.sii_result == 'Rechazado' or not inv.sii_xml_dte: #Retimbrar con número de atención y envío
                inv._timbrar(n_atencion)
            #@TODO Mejarorar esto en lo posible
            if not inv.document_class_id.sii_code in clases:
                clases[inv.document_class_id.sii_code] = []
            clases[inv.document_class_id.sii_code].extend([{
                                                'id': inv.id,
                                                'envio': inv.sii_xml_dte,
                                                'sii_batch_number': inv.sii_batch_number,
                                                'sii_document_number': inv.sii_document_number
                                            }])
            if not company_id:
                company_id = inv.company_id
            elif company_id.id != inv.company_id.id:
                raise UserError("Está combinando compañías, no está permitido hacer eso en un envío")
        file_name = ""
        dtes = {}
        SubTotDTE = ''
        resol_data = self.get_resolution_data(company_id)
        signature_id = self.env.user.get_digital_signature(company_id)
        RUTEmisor = self.format_vat(company_id.vat)
        for id_class_doc, classes in clases.items():
            NroDte = 0
            for documento in classes:
                if documento['sii_batch_number'] in dtes.keys():
                    raise UserError("No se puede repetir el mismo número de orden")
                dtes.update({str(documento['sii_batch_number']): documento['envio']})
                NroDte += 1
                file_name += 'F' + str(documento['sii_document_number']) + 'T' + str(id_class_doc)
            SubTotDTE += '<SubTotDTE>\n<TpoDTE>' + str(id_class_doc) + '</TpoDTE>\n<NroDTE>'+str(NroDte)+'</NroDTE>\n</SubTotDTE>\n'
        file_name += ".xml"
        documentos = ""
        for key in sorted(dtes.keys(), key=lambda r: int(r)):
            documentos += '\n'+dtes[key]
        # firma del sobre
        dtes = self.create_template_envio(
                RUTEmisor,
                RUTRecep,
                resol_data['dte_resolution_date'],
                resol_data['dte_resolution_number'],
                self.time_stamp(),
                documentos,
                signature_id.subject_serial_number,
                SubTotDTE,
            )
        env = 'env'
        if es_boleta:
            envio_dte = self.create_template_env_boleta(dtes)
            env = 'env_boleta'
        else:
            envio_dte = self.create_template_env(dtes)
        envio_dte = self.env['account.invoice'].sudo(self.env.uid).with_context({'company_id': company_id.id}).sign_full_xml(
                envio_dte.replace('<?xml version="1.0" encoding="ISO-8859-1"?>\n', ''),
                'SetDoc',
                env,
            )
        return {
                'xml_envio': '<?xml version="1.0" encoding="ISO-8859-1"?>\n' + envio_dte,
                'name': file_name,
                'company_id': company_id.id,
                'user_id': self.env.uid,
                }

    @api.multi
    def do_dte_send(self, n_atencion=None):
        tipo_envio = {
                'boleta': [],
                'normal': [],
            }
        for r in self:
            if r._es_boleta():
                tipo_envio['boleta'].append(r.id)
            else:
                tipo_envio['normal'].append(r.id)
            if r.sii_result in ['Rechazado'] or (r.company_id.dte_service_provider == 'SIICERT' and r.sii_xml_request.state in ['', 'NoEnviado']):
                if r.sii_xml_request:
                    if len(r.sii_xml_request.invoice_ids) == 1:
                        r.sii_xml_request.unlink()
                    else:
                        r.sii_xml_request = False
                r.sii_message = ''
        for k, t in tipo_envio.items():
            if not t:
                continue
            recs = self.browse(t)
            envio_id = recs[0].sii_xml_request
            if not envio_id:
                envio = recs._crear_envio(n_atencion, RUTRecep="60803000-K")
                if k in ['boleta']:
                    envio.update({
                        'state': 'Aceptado',
                        'sii_send_ident': envio['name'],
                    })
                envio_id = self.env['sii.xml.envio'].create(envio)
                for r in recs:
                    r.sii_xml_request = envio_id.id
                    if r._es_boleta():
                        r.sii_result = 'Proceso'
            if k in ['normal']:
                resp = envio_id.send_xml()
        return envio_id

    def process_response_xml(self, resp):
        if resp['SII:RESPUESTA']['SII:RESP_HDR']['ESTADO'] == '2':
            status = {'warning':{'title':_("Error code: 2"), 'message': _(resp['SII:RESPUESTA']['SII:RESP_HDR']['GLOSA'])}}
            return "Enviado"
        if resp['SII:RESPUESTA']['SII:RESP_HDR']['ESTADO'] in ["EPR", "MMC", "DOK", "TMC", "AND", "MMD"]:
            return "Proceso"
        elif resp['SII:RESPUESTA']['SII:RESP_HDR']['ESTADO'] in ["DNK"]:
            return "Reparo"
        elif resp['SII:RESPUESTA']['SII:RESP_HDR']['ESTADO'] in ["FAU", "RCT", "FNA"]:
            return "Rechazado"
        elif resp['SII:RESPUESTA']['SII:RESP_HDR']['ESTADO'] in ["FAN", "ANC"]:
            return "Anulado"  #Desde El sii o por NC

    @api.onchange('sii_message')
    def get_sii_result(self):
        for r in self:
            if r._es_boleta():
                r.sii_result = "Proceso"
                continue
            if r.sii_message:
                r.sii_result = r.process_response_xml(
                                    xmltodict.parse(r.sii_message))
                continue
            if r.sii_xml_request.state == 'NoEnviado':
                r.sii_result = 'EnCola'
                continue
            r.sii_result = r.sii_xml_request.state

    def _get_dte_status(self):
        for r in self:
            if r._es_boleta():
                continue
            if r.sii_xml_request and r.sii_xml_request.state not in ['Aceptado', 'Rechazado']:
                continue
            token = r.sii_xml_request.get_token(self.env.user, r.company_id)
            url = server_url[r.company_id.dte_service_provider] + 'QueryEstDte.jws?WSDL'
            _server = Client(url)
            receptor = r.format_vat(r.commercial_partner_id.vat)
            date_invoice = datetime.strptime(r.date_invoice, "%Y-%m-%d").strftime("%d-%m-%Y")
            signature_id = self.env.user.get_digital_signature(r.company_id)
            rut = signature_id.subject_serial_number
            try:
                respuesta = _server.service.getEstDte(
                    rut[:8].replace('-', ''),
                    str(rut[-1]),
                    r.company_id.vat[2:-1],
                    r.company_id.vat[-1],
                    receptor[:8].replace('-', ''),
                    receptor[-1],
                    str(r.document_class_id.sii_code),
                    str(r.sii_document_number),
                    date_invoice,
                    str(int(r.amount_total)),
                    token,
                )
            except Exception as e:
                msg = "Error al obtener Estado DTE"
                _logger.warning("%s: %s" % (msg, str(e)))
                if e.args[0][0] == 503:
                    raise UserError('%s: Conexión al SII caída/rechazada o el SII está temporalmente fuera de línea, reintente la acción' % (msg))
                raise UserError(("%s: %s" % (msg, str(e))))
            r.sii_message = respuesta

    @api.multi
    def ask_for_dte_status(self):
        for r in self:
            if not r.sii_xml_request and not r.sii_xml_request.sii_send_ident:
                raise UserError('No se ha enviado aún el documento, aún está en cola de envío interna en odoo')
            if r.sii_xml_request.state not in ['Aceptado', 'Rechazado']:
                r.sii_xml_request.get_send_status(r.env.user)
        if r.sii_xml_request.state in ['Aceptado', 'Rechazado']:
            try:
                self._get_dte_status()
            except Exception as e:
                _logger.warning("Error al obtener DTE Status: %s" %str(e))
        self.get_sii_result()
        for r in self:
            mess = False
            if r.sii_result == 'Rechazado':
                mess = {
                            'title': "Documento Rechazado",
                            'message': "%s" % r.name,
                            'type': 'dte_notif',
                        }
            if r.sii_result == 'Anulado':
                r.canceled = True
                try:
                    r.action_invoice_cancel()
                except:
                    _logger.warning("Error al cancelar Documento")
                mess = {
                            'title': "Documento Anulado",
                            'message': "%s" % r.name,
                            'type': 'dte_notif',
                        }
            if mess:
                self.env['bus.bus'].sendone((self._cr.dbname,
                                            'account.invoice',
                                            r.user_id.partner_id.id),
                                            mess)

    def set_dte_claim(self, rut_emisor=False, company_id=False,
                      sii_document_number=False, document_class_id=False,
                      claim=False):
        rut_emisor = rut_emisor or self.format_vat(self.company_id.partner_id.vat)
        company_id = company_id or self.company_id
        sii_document_number = sii_document_number
        document_class_id = document_class_id or self.document_class_id
        claim = claim or self.claim
        token = self.sii_xml_request.get_token(self.env.user, self.company_id)
        url = claim_url[company_id.dte_service_provider] + '?wsdl'
        _server = Client(
            url,
            headers={
                'Cookie': 'TOKEN=' + token,
                },
        )
        try:
            respuesta = _server.service.ingresarAceptacionReclamoDoc(
                rut_emisor[:-2],
                rut_emisor[-1],
                str(document_class_id.sii_code),
                str(sii_document_number),
                claim,
            )
        except Exception as e:
            msg = "Error al ingresar Reclamo"
            _logger.warning("%s: %s" % (msg, str(e)))
            if e.args[0][0] == 503:
                raise UserError('%s: Conexión al SII caída/rechazada o el SII está temporalmente fuera de línea, reintente la acción' % (msg))
            raise UserError(("%s: %s" % (msg, str(e))))
        if self.id:
            self.claim_description = respuesta

    @api.multi
    def get_dte_claim(self, ):
        token = self.sii_xml_request.get_token(self.env.user, self.company_id)
        url = claim_url[self.company_id.dte_service_provider] + '?wsdl'
        _server = Client(
            url,
            headers={
                'Cookie': 'TOKEN=' + token,
                },
        )
        respuesta = _server.service.listarEventosHistDoc(
            self.company_id.vat[2:-1],
            self.company_id.vat[-1],
            str(self.document_class_id.sii_code),
            str(self.sii_document_number),
        )
        self.claim_description = respuesta
        #resp = xmltodict.parse(respuesta)
        #if resp['SII:RESPUESTA']['SII:RESP_HDR']['ESTADO'] == '2':
        #    status = {'warning':{'title':_("Error code: 2"), 'message': _(resp['SII:RESPUESTA']['SII:RESP_HDR']['GLOSA'])}}
        #    return status
        #if resp['SII:RESPUESTA']['SII:RESP_HDR']['ESTADO'] == "EPR":
        #    self.sii_result = "Proceso"
        #    if resp['SII:RESPUESTA']['SII:RESP_BODY']['RECHAZADOS'] == "1":
        #        self.sii_result = "Rechazado"
        #    if resp['SII:RESPUESTA']['SII:RESP_BODY']['REPARO'] == "1":
        #        self.sii_result = "Reparo"
        #elif resp['SII:RESPUESTA']['SII:RESP_HDR']['ESTADO'] == "RCT":
        #    self.sii_result = "Rechazado"

    @api.multi
    def wizard_upload(self):
        return {
                'type': 'ir.actions.act_window',
                'res_model': 'sii.dte.upload_xml.wizard',
                'src_model': 'account.invoice',
                'view_mode': 'form',
                'view_type': 'form',
                'views': [(False, 'form')],
                'target': 'new',
                'tag': 'action_upload_xml_wizard'
                }

    @api.multi
    def wizard_validar(self):
        return {
                'type': 'ir.actions.act_window',
                'res_model': 'sii.dte.validar.wizard',
                'src_model': 'account.invoice',
                'view_mode': 'form',
                'view_type': 'form',
                'views': [(False, 'form')],
                'target': 'new',
                'tag': 'action_validar_wizard'
                }

    @api.multi
    def invoice_print(self):
        self.ensure_one()
        self.sent = True
        return self.env.ref('account.account_invoices').report_action(self)

    @api.multi
    def print_cedible(self):
        """ Print Cedible
        """
        return self.env.ref('l10n_cl_fe.action_print_cedible').report_action(self)

    @api.multi
    def print_copy_cedible(self):
        """ Print Copy and Cedible
        """
        return self.env.ref('l10n_cl_fe.action_print_copy_cedible').report_action(self)

    def send_exchange(self):
        att = self._create_attachment()
        body = 'XML de Intercambio DTE: %s' % (self.number)
        subject = 'XML de Intercambio DTE: %s' % (self.number)
        dte_email_id = self.company_id.dte_email_id or self.env.user.company_id.dte_email_id
        dte_receptors = self.commercial_partner_id.child_ids + self.commercial_partner_id
        email_to = ''
        for dte_email in dte_receptors:
            if not dte_email.send_dte or not dte_email.email:
                continue
            if dte_email.email.lower() in ['facturacionmipyme2@sii.cl',
                                   'facturacionmipyme@sii.cl']:
                resp = self.env['sii.respuesta.cliente'].sudo().search([
                        ('exchange_id', '=', att.id)])
                resp.estado = '0'
                continue
            email_to += dte_email.email + ','
        if email_to == '':
            return
        values = {
                'res_id': self.id,
                'email_from': dte_email_id.name_get()[0][1],
                'email_to': email_to[:-1],
                'auto_delete': False,
                'model': 'account.invoice',
                'body': body,
                'subject': subject,
                'attachment_ids': [[6, 0, att.ids]],
            }
        send_mail = self.env['mail.mail'].sudo().create(values)
        send_mail.send()

    @api.multi
    def manual_send_exchange(self):
        self.send_exchange()

    @api.multi
    def _get_printed_report_name(self):
        self.ensure_one()
        if self.document_class_id:
            string_state = ""
            if self.state == 'draft':
                string_state = "en borrador "
            report_string = "%s %s %s" % (self.document_class_id.name,
                                          string_state,
                                          self.sii_document_number or '')
        else:
            report_string = super(AccountInvoice, self)._get_printed_report_name()
        return report_string

    @api.multi
    def getTotalDiscount(self):
        total_discount = 0
        for l in self.invoice_line_ids:
            total_discount +=  (((l.discount or 0.00) /100) * l.price_unit * l.quantity)
        return self.currency_id.round(total_discount)

    @api.multi
    def sii_header(self):
        W, H = (560, 255)
        img = Image.new('RGB', (W, H), color=(255,255,255))

        d = ImageDraw.Draw(img)
        w, h = (0, 0)
        for i in range(10):
            d.rectangle(((w, h), (550+w, 220+h)), outline="black")
            w += 1
            h += 1
        font = ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf', 40)
        d.text((50,30), "R.U.T.: %s" % self.company_id.document_number, fill=(0,0,0), font=font)
        d.text((50,90), self.document_class_id.name, fill=(0,0,0), font=font)
        d.text((220,150), "N° %s" % self.sii_document_number, fill=(0,0,0), font=font)
        font = ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf', 20)
        d.text((200,235), "SII %s" %self.company_id.sii_regional_office_id.name, fill=(0,0,0), font=font)

        buffered = BytesIO()
        img.save(buffered, format="PNG")
        imm = base64.b64encode(buffered.getvalue()).decode()
        return imm
