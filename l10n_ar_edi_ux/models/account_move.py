from odoo import models, fields, api, _
from odoo.addons.l10n_ar_edi.models.account_move import WS_DATE_FORMAT
from odoo.exceptions import UserError


class AccountMove(models.Model):
    _inherit = "account.move"

    l10n_ar_afip_asoc_period_start = fields.Date(
        string='Associated Period Start',
        states={'draft': [('readonly', False)]},
        help='Set this field if it is you are reporting debit/credit note and have not related invoice.'
        ' IMPORTANT: This is only applies on "Electronic Invoice - Web Service"')
    l10n_ar_afip_asoc_period_end = fields.Date(
        string='Associated Perdio End',
        states={'draft': [('readonly', False)]},
        help='Set this field if it is you are reporting debit/credit note and have not related invoice.'
        ' IMPORTANT: This is only applies on "Electronic Invoice - Web Service"')
    l10n_ar_boarding_permission_ids = fields.Many2many('l10n_ar.boarding_permission', string="Permiso de Embarque", check_company=True, ondelete='restrict', help="Solo se envía esta información si la factura es de exportación y el 'Concepto AFIP' es 'Productos / Exportación definitiva de bienes'")

    def _found_related_invoice(self):
        """
        TODO borrar cuando se mezcle https://github.com/odoo/enterprise/pull/12972/files
        """
        res = super()._found_related_invoice()
        if not res and self.l10n_latam_document_type_id.internal_type in ['credit_note', 'debit_note'] and \
           self.sudo().env.ref('base.module_sale').state == 'installed':
            original_entry = self.mapped('invoice_line_ids.sale_line_ids.invoice_lines').filtered(
                lambda x:  x.move_id.l10n_latam_document_type_id.country_id.code == 'AR' and
                x.move_id.l10n_latam_document_type_id.internal_type != self.l10n_latam_document_type_id.internal_type
                and x.move_id.l10n_ar_afip_result in ['A', 'O'] and x.move_id.l10n_ar_afip_auth_code).mapped('move_id')
            return original_entry and original_entry[0] or res
        return res

    @api.model
    def wsfe_get_cae_request(self, client=None):
        res = super().wsfe_get_cae_request(client=client)
        if self.l10n_latam_document_type_id.internal_type in ['credit_note', 'debit_note']:
            related_invoices = self._get_related_invoice_data()
            if not related_invoices and self.l10n_ar_afip_asoc_period_start and self.l10n_ar_afip_asoc_period_end:
                res.get('FeDetReq')[0].get('FECAEDetRequest').update({'PeriodoAsoc': {
                    'FchDesde': self.l10n_ar_afip_asoc_period_start.strftime(WS_DATE_FORMAT['wsfe']),
                    'FchHasta': self.l10n_ar_afip_asoc_period_end.strftime(WS_DATE_FORMAT['wsfe'])}})
        return res

    def _post(self, soft=True):
        """ Be able to validate electronic vendor bills that are type AFIP POS """
        purchase_ar_edi_invoices = self.filtered(lambda x: x.journal_id.type == 'purchase' and x.journal_id.l10n_ar_is_pos and x.journal_id.l10n_ar_afip_ws)

        # Send invoices to AFIP and get the return info
        validated = error_vendor_bill = self.env['account.move']
        for bill in purchase_ar_edi_invoices:

            # If we are on testing environment and we don't have certificates we validate only locally.
            # This is useful when duplicating the production database for training purpose or others
            if bill._is_dummy_afip_validation():
                bill._dummy_afip_validation()
                super(AccountMove, bill)._post(soft=soft)
                validated += bill
                continue

            client, auth, transport = bill.company_id._l10n_ar_get_connection(bill.journal_id.l10n_ar_afip_ws)._get_client(return_transport=True)
            super(AccountMove, bill)._post(soft=soft)
            return_info = bill._l10n_ar_do_afip_ws_request_cae(client, auth, transport)
            if return_info:
                error_vendor_bill = bill
                break
            validated += bill

            # If we get CAE from AFIP then we make commit because we need to save the information returned by AFIP
            # in Odoo for consistency, this way if an error ocurrs later in another invoice we will have the ones
            # correctly validated in AFIP in Odoo (CAE, Result, xml response/request).
            if not self.env.context.get('l10n_ar_invoice_skip_commit'):
                self._cr.commit()

        if error_vendor_bill:
            msg = _('We could not validate the vendor bill in AFIP') + (' "%s" %s. ' % (
                error_vendor_bill.partner_id.name, error_vendor_bill.display_name) if error_vendor_bill.exists() else '. ') + _(
                    'This is what we get:\n%s\n\nPlease make the required corrections and try again') % (return_info)
            # if we've already validate any invoice, we've commit and we want to inform which invoices were validated
            # which one were not and the detail of the error we get. This ins neccesary because is not usual to have a
            # raise with changes commited on databases
            if validated:
                unprocess = self - validated - error_vendor_bill
                msg = _('Some vendor bills where validated in AFIP but as we have an error with one vendor bill the batch validation was stopped\n'
                        '\n* These vendor bills were validated:\n   * %s\n' % ('\n   * '.join(validated.mapped('name'))) +
                        '\n* These vendor bills weren\'t validated:\n%s\n' % ('\n'.join(['   * %s: "%s" amount %s' % (
                            item.display_name, item.partner_id.name, item.amount_total_signed) for item in unprocess])) + '\n\n\n' + msg)
            raise UserError(msg)

        return super(AccountMove, self - purchase_ar_edi_invoices)._post(soft=soft)

    def _get_permissions(self):
        """ Get 'permiso de embarque' for foreign invoices. """
        self.ensure_one()
        res = []
        invalid_permissions = self.check_valid_boarding_permission()
        if invalid_permissions:
            invalid_permissions_str = '\n'.join(invalid_permissions)
            raise UserError(_('Invalid boarding permissions:\n %s') % invalid_permissions_str)
        for permiso in self.l10n_ar_boarding_permission_ids:
            res.append({'Id_permiso': permiso.number, 'Dst_merc': permiso.dst_country.l10n_ar_afip_code})
        return res

    @api.model
    def wsfex_get_cae_request(self, last_id, client):
        """ Set permiso de embarque to foreign invoice. """
        res = super(AccountMove, self).wsfex_get_cae_request(last_id, client)
        if int(self.l10n_latam_document_type_id.code) == 19 and int(self.l10n_ar_afip_concept) == 1:
            ArrayOfPermisions = client.get_type('ns0:ArrayOfPermiso')
            permisos = self._get_permissions()
            permiso_existente = "S" if permisos else "N"
            res.update({'Permisos': ArrayOfPermisions(permisos) if permisos else None})
            res.update({'Permiso_existente': permiso_existente})
        return res

    def check_valid_boarding_permission(self):
        """ This method is used to verify that the Permisos de embarque entered on the export invoice are valid. Receives the authentication credentials, cuit of the represented user, código de despacho and destination country and verifies their existence in the base de datos aduanera. """
        client, auth = self.company_id._l10n_ar_get_connection(self.journal_id.l10n_ar_afip_ws)._get_client()
        valid_permissions = []
        invalid_permissions = []
        for perm in self.l10n_ar_boarding_permission_ids:
            response = client.service['FEXCheck_Permiso'](auth, ID_Permiso=perm.number, Dst_merc=int(perm.dst_country.l10n_ar_afip_code))
            permission_status = response['FEXResultGet']['Status']
            if permission_status == 'OK':
                valid_permissions.append(perm.display_name)
            else:
                invalid_permissions.append(perm.display_name)
        valid_permissions_str = ', '.join(valid_permissions)
        invalid_permissions_str = ', '.join(invalid_permissions)
        msg = _('Valid boarding permissions: %s') % valid_permissions_str + _('. Invalid boarding permissions: %s') % invalid_permissions_str
        self.message_post(body=msg)
        return invalid_permissions
