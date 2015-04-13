import requests
import email.Message
import email.utils
import hashlib
import as2utils
import traceback
import os 
import re
import sys
import base64
from email.mime.multipart import MIMEMultipart
from email.parser import HeaderParser
from M2Crypto import BIO, Rand, SMIME, X509
from django.conf import settings
from pyas2 import models
from pyas2 import init

def save_message(message, raw_payload):
    try:
        payload = email.message_from_string(raw_payload)
        models.Log.objects.create(message=message, status='S', text='Begin Processing of received AS2 message')
        if not models.Organization.objects.filter(as2_name=as2utils.unescape_as2name(payload.get('as2-to'))).exists():
            raise as2partnernotfound('Unknown AS2 organization with id %s'%payload.get('as2-to'))
        message.organization = models.Organization.objects.get(as2_name=as2utils.unescape_as2name(payload.get('as2-to')))
        if not models.Partner.objects.filter(as2_name=as2utils.unescape_as2name(payload.get('as2-from'))).exists():
            raise as2partnernotfound('Unknown AS2 Trading partner with id %s'%payload.get('as2-from'))
        message.partner = models.Partner.objects.get(as2_name=as2utils.unescape_as2name(payload.get('as2-from')))
        models.Log.objects.create(
            message=message, 
            status='S', 
            text='Message is for Organization "%s" from partner "%s"'%(message.organization, message.partner)
        )
        micContent = payload.get_payload()
        filename = payload.get_filename()
        if message.partner.encryption and payload.get_content_type() != 'application/pkcs7-mime':
            raise as2insufficientsecurity('Incoming messages from AS2 partner %s are defined to be encrypted'%message.partner.as2_name)	
        if payload.get_content_type() == 'application/pkcs7-mime' and payload.get_param('smime-type') == 'enveloped-data':
            models.Log.objects.create(message=message, status='S', text='Begin Decrypting the payload using private key %s'%message.organization.encryption_key)
            message.encrypted = True            
            try:
                payload.get_payload().encode('ascii')
            except Exception,e:			
                payload.set_payload(payload.get_payload().encode('base64'))
            try:
                decrypted_content = as2utils.decrypt_payload(
                    as2utils.mimetostring(payload,78),
                    as2utils.join(init.gsettings['root_dir'], message.organization.encryption_key.certificate.name),
                    str(message.organization.encryption_key.certificate_passphrase)
                )
                micContent,raw_payload = decrypted_content,decrypted_content
                payload = email.message_from_string(decrypted_content)
                if payload.get_content_type() == 'text/plain':
                    payload = email.Message.Message()
                    payload.set_payload(decrypted_content)
                    payload.set_type('application/edi-consent')
                    if filename:
                        payload.add_header('Content-Disposition', 'attachment', filename=filename)
            except Exception, msg:
                raise as2decryptionfailed('Failed to decrypt message, exception message is %s' %msg)
        if message.partner.signature and payload.get_content_type() != 'multipart/signed':
            raise as2insufficientsecurity('Incoming messages from AS2 partner %s are defined to be signed'%message.partner.as2_name)
        if payload.get_content_type() == 'multipart/signed':
            if not message.partner.signature_key:
                raise as2insufficientsecurity('Partner has no signature varification key defined')
            models.Log.objects.create(message=message, status='S', text='Begin Verifying the signature using public key %s'%message.partner.signature_key)
            message.signed = True
            main_boundary = '--' + payload.get_boundary()
            for part in payload.walk():
                if part.get_content_type() == "application/pkcs7-signature":
                    try:
                        raw_sig = part.get_payload().encode('ascii').strip()
                    except Exception,e:
                        raw_sig = part.get_payload().encode('base64').strip()
                else:
                    payload = part
                    msg = as2utils.canonicalize(part)
                    micContent = msg
            verify_cert =  str(message.partner.signature_key.certificate.path)
            try:
                as2utils.verify_payload(raw_payload,None,verify_cert)
            except Exception, e:
                try:
                    as2utils.verify_payload(raw_payload.split(main_boundary)[1].strip(),raw_sig,verify_cert)
                except Exception, e:
                    try:
                        as2utils.verify_payload(msg, raw_sig,verify_cert)
                    except Exception, e:
                        raise as2invalidsignature('Signature Verification Failed, exception message is %s'%str(e))
        if payload.get_content_type() == 'application/pkcs7-mime' and payload.get_param('smime-type') == 'compressed-data':
            models.Log.objects.create(message=message, status='S', text='Decompressing the payload')
            message.compressed = True
            try:
                payload.get_payload().encode('ascii')
                cdata = base64.b64decode(payload.get_payload())
            except Exception,e:
                cdata = payload.get_payload()
            try:
                dcontent = as2utils.decompress_payload(cdata)
                if not message.signed :
                    micContent = dcontent
                payload = email.message_from_string(dcontent)
            except Exception, e:
                raise as2decompressionfailed('Failed to decompress message,exception message is %s' %e) 
        calcMIC = getattr(hashlib, message.partner.signature or 'sha1')
        message.mic = calcMIC(micContent).digest().encode('base64').strip()
        return payload
    finally:
        message.save()	
	
def build_mdn(message, status, **kwargs):
    try:
        hparser = HeaderParser()
        message_header = hparser.parsestr(message.headers)
        text = "The AS2 message has been processed. Thank you for exchanging AS2 messages with Pyas2."
        if status != 'success':
            text = "The AS2 message could not be processed. The disposition-notification message has additional details."
            models.Log.objects.create(message=message, status='E', text = kwargs['status_message'])
            message.status = 'E'
            message.adv_status = kwargs['status_message']
        else:
            message.status = 'S'
        if not message_header.get('disposition-notification-to'):
            models.Log.objects.create(message=message, status='S', text='MDN not requested by partner, closing request.')
            return None, None
        models.Log.objects.create(message=message, status='S', text='Building MDN to be sent back to partner')
        main = MIMEMultipart('report', report_type="disposition-notification")
        textmessage = email.Message.Message()
        textmessage.set_payload("%s\n"%text)
        textmessage.set_type('text/plain')
        textmessage.set_charset('us-ascii')
        del textmessage['MIME-Version']
        main.attach(textmessage)
        mdnbase = email.Message.Message()
        mdnbase.set_type('message/disposition-notification')
        mdnbase.set_charset('us-ascii')
        mdn = 'Reporting-UA: Bots Opensource EDI Translator\n'
        mdn = mdn + 'Original-Recipient: rfc822; %s\n'%message_header.get('as2-to')
        mdn = mdn + 'Final-Recipient: rfc822; %s\n'%message_header.get('as2-to')
        mdn = mdn + 'Original-Message-ID: <%s>\n'%message.message_id
        if status != 'success':
            mdn = mdn + 'Disposition: automatic-action/MDN-sent-automatically; processed/%s: %s\n'%(status, kwargs['adv_status'])
        else:
            mdn = mdn + 'Disposition: automatic-action/MDN-sent-automatically; processed\n'
        if message.mic:
            mdn = mdn + 'Received-content-MIC: %s, sha1\n'%message.mic
        mdnbase.set_payload(mdn)
        del mdnbase['MIME-Version']
        main.attach(mdnbase)
        del main['MIME-Version']
        models.Log.objects.create(message=message, status='S', text='MDN report created successfully and attached to the main message')
        mdnsigned = False
        if message_header.get('disposition-notification-options') and message.organization and message.organization.signature_key: 
            models.Log.objects.create(message=message, status='S', text='Signing the MDN using private key %s'%message.organization.signature_key)
            mdnsigned = True
            options = message_header.get('disposition-notification-options').split(";")
            algorithm = options[1].split(",")[1].strip()
            signed = MIMEMultipart('signed', protocol="application/pkcs7-signature", micalg='sha1')
            signed.attach(main)
            signature = as2utils.sign_payload(
                as2utils.mimetostring(main, 0) + '\n', 
                as2utils.join(init.gsettings['root_dir'], message.organization.signature_key.certificate.name), 
                str(message.organization.signature_key.certificate_passphrase)
            )
            signed.attach(signature)
            mdnmessage = signed
        else:
            mdnmessage = main
        mdnbody = as2utils.extractpayload(mdnmessage)
        mainboundary = '--' + main.get_boundary() + '--'
        mdnbody = mdnbody.replace(mainboundary, mainboundary + '\n').replace('\n','\r\n')
        mdnmessage.add_header('ediint-features', 'CEM')
        mdnmessage.add_header('as2-from', message_header.get('as2-to'))
        mdnmessage.add_header('as2-to', message_header.get('as2-from')) 
        mdnmessage.add_header('AS2-Version', '1.2')
        mdnmessage.add_header('date', email.Utils.formatdate(localtime=True))
        mdnmessage.add_header('Message-ID', email.utils.make_msgid())
        mdnmessage.add_header('user-agent', 'PYAS2, A pythonic AS2 server')
        outputdir = init.gsettings['mdn_send_store']
        filename = mdnmessage.get('message-id').strip('<>') + '.mdn'
        fullfilename = as2utils.join(outputdir, filename)
        file = open(fullfilename , 'wb')
        file.write(mdnbody)
        file.close()
        mdn_headers = ''
        for key in mdnmessage.keys():
            mdn_headers = mdn_headers + '%s: %s\n'%(key, mdnmessage[key])
        if message_header.get('receipt-delivery-option'):
            message.mdn = models.MDN.objects.create(
                message_id=mdnmessage['message-id'].strip('<>'),
                file=fullfilename, 
                status='P',
                signed=mdnsigned, 
                headers=mdn_headers, 
                return_url= message_header['receipt-delivery-option']
            )
            message.mdn_mode = 'ASYNC'
            mdnbody, mdnmessage = None, None
            models.Log.objects.create(message=message, status='S', text='Asynchronous MDN requested, setting status to pending')
        else:
            message.mdn = models.MDN.objects.create(message_id=mdnmessage.get('message-id').strip('<>'),file=fullfilename, status='S', signed=mdnsigned, headers=mdn_headers)
            message.mdn_mode = 'SYNC'
            models.Log.objects.create(message=message, status='S', text='MDN created successfully, sending it to partner')
        return mdnbody, mdnmessage
    finally:
        message.save()	

def build_message(message):
    models.Log.objects.create(message=message, status='S', text='Build the AS2 message and header to send to the partner')
    reference = '<%s>'%message.message_id
    email_datetime = email.Utils.formatdate(localtime=True)
    as2Header = {
        'AS2-Version'         : '1.2',
        'ediint-features'     : 'CEM',
        'MIME-Version'        : '1.0',  
        'Message-ID'          : reference,
        'AS2-From'            : as2utils.escape_as2name(message.organization.as2_name),
        'AS2-To'              : as2utils.escape_as2name(message.partner.as2_name),
        'Subject'             : message.partner.subject,
        'Date'                : email_datetime,
        'recipient-address'   : message.partner.target_url,
        'user-agent'          : 'PYAS2, A pythonic AS2 server'
    }
    payload = email.Message.Message()
    with open(message.payload.file, 'rb') as fh:
        payload.set_payload(fh.read())
        fh.close()
    payload.set_type(message.partner.content_type)
    payload.add_header('Content-Disposition', 'attachment', filename=message.payload.name)
    del payload['MIME-Version']
    micContent,cmicContent,content = payload.get_payload(),None,payload.get_payload()
    if message.partner.compress:
        models.Log.objects.create(message=message, status='S', text='Compressing the AS2 message.')
        message.compressed = True
        micContent = as2utils.mimetostring(payload, 0).replace('\n','\r\n')
        cmessage = email.Message.Message()
        cmessage.set_type('application/pkcs7-mime')
        cmessage.set_param('name', 'smime.p7z')
        cmessage.set_param('smime-type', 'compressed-data') 
        cmessage.add_header('Content-Transfer-Encoding', 'base64')
        cmessage.add_header('Content-Disposition', 'attachment', filename='smime.p7z')
        cmessage.set_payload(as2utils.compress_payload(as2utils.mimetostring(payload, 0).replace('\n','\r\n')))
        content,payload = cmessage.get_payload(),cmessage
    if message.partner.signature: 
        models.Log.objects.create(message=message, status='S', text='Signing the AS2 message using organzation key %s'%message.organization.signature_key)
        message.signed = True
        multipart = MIMEMultipart('signed',protocol="application/pkcs7-signature",micalg=message.partner.signature)
        del multipart['MIME-Version']
        micContent = as2utils.mimetostring(payload, 0).replace('\n','\r\n') 
        multipart.attach(payload)
        signature = as2utils.sign_payload(
            micContent,
            #as2utils.canonicalize(payload), 
            as2utils.join(init.gsettings['root_dir'], message.organization.signature_key.certificate.name),
            str(message.organization.signature_key.certificate_passphrase)
        )
        multipart.attach(signature)
        multipart.as_string()
        content = as2utils.extractpayload(multipart).replace('\n','\r\n')
        #content = '--%s\r\n'%multipart.get_boundary() + encmicContent + '--%s\r\n'%multipart.get_boundary() + signature.as_string() + '\r\n--%s--'%multipart.get_boundary()
        payload = multipart
    if message.partner.encryption: 
        models.Log.objects.create(message=message, status='S', text='Encrypting the AS2 message using partner key %s'%message.partner.encryption_key)
        message.encrypted = True
        #init.logger.debug("MEssage -%s"%as2utils.mimetostring(payload, 0))
        payload = as2utils.encrypt_payload(as2utils.mimetostring(payload, 0), message.partner.encryption_key.certificate.path , message.partner.encryption)
        payload.set_type('application/pkcs7-mime')
        content = payload.get_payload()
    if message.partner.mdn:
        as2Header['disposition-notification-to'] = 'no-reply@pyas2.com' 
        if message.partner.mdn_sign:			
            as2Header['disposition-notification-options'] = 'signed-receipt-protocol=required, pkcs7-signature; signed-receipt-micalg=optional, %s'%message.partner.mdn_sign
        message.mdn_mode = 'SYNC'
        if message.partner.mdn_mode == 'ASYNC':
            as2Header['receipt-delivery-option'] = init.gsettings['mdn_url']
            message.mdn_mode = 'ASYNC'
    calcMIC = getattr(hashlib, message.partner.signature or 'sha1')
    message.mic = calcMIC(micContent).digest().encode('base64').strip()
    as2Header.update(payload.items())
    message.headers = ''
    for key in as2Header:
        message.headers = message.headers + '%s: %s\n'%(key, as2Header[key])
    message.save()
    models.Log.objects.create(message=message, status='S', text='AS2 message has been built successfully, sending it to the partner')
    return content 

def send_message(message, payload):
    try:
        hparser = HeaderParser()
        message_header = hparser.parsestr(message.headers)
        auth = None
        if message.partner.http_auth:
            auth = (message.partner.http_auth_user, message.partner.http_auth_pass)
        try:
            response = requests.post(message.partner.target_url,  auth = auth, headers = dict(message_header.items()), data = payload)
            response.raise_for_status()
        except Exception,e:
            message.status = 'R'
            models.Log.objects.create(message=message, status='E', text='Message send failed with error %s'%e)
            message.adv_status = 'Message send failed due to communcation error'
            return
        models.Log.objects.create(message=message, status='S', text='AS2 message sent to the partner, checking for mdn if requested')
        if message.partner.mdn:
            if message.partner.mdn_mode == 'ASYNC':
                models.Log.objects.create(message=message, status='S', text='ASYNC MDN requested, waiting for partner to send it ........')
                message.adv_status = 'Waiting for asynchronous MDN'
                message.status = 'P'
                return
            mdnContent = '';
            for key in response.headers:
                mdnContent = mdnContent + '%s: %s\n'%(key, response.headers[key])
            mdnContent = mdnContent + '\n' + response.content
            models.Log.objects.create(message=message, status='S', text='Processing synchronous mdn received from partner')
            save_mdn(message, mdnContent)
        else:
            message.status = 'S'
            message.adv_status = 'Completed'
            models.Log.objects.create(message=message, status='S', text='No MDN requested, File Transferred successfully to the partner')
    finally:
        message.save()

def save_mdn(message, mdnContent):
    try:
        mdnMessage = email.message_from_string(mdnContent)
        mdnHeaders = ''
        for key in mdnMessage.keys():
            mdnHeaders = mdnHeaders + '%s: %s\n'%(key, mdnMessage[key])
        messageId = mdnMessage.get('message-id')
        if message.partner.mdn_sign and mdnMessage.get_content_type() != 'multipart/signed':
            raise as2exception("Expected signed MDN but unsigned MDN returned")
        mdnsigned = False
        if mdnMessage.get_content_type() == 'multipart/signed':
            models.Log.objects.create(message=message, status='S', text='Verifying the signed MDN with partner key %s'%message.partner.signature_key)
            mdnsigned = True
            main_boundary = '--' + mdnMessage.get_boundary()
            for part in mdnMessage.get_payload():
                if part.get_content_type().lower() == "application/pkcs7-signature":
                    temp_sig = part
                else:
                    temp_msg = part
            try:
                raw_sig = temp_sig.get_payload().encode('ascii').strip()
            except Exception,e:
                raw_sig = temp_sig.get_payload().encode('base64').strip()
            mdnMessage = temp_msg
            verify_cert = as2utils.join(init.gsettings['root_dir'], message.partner.signature_key.certificate.name)
            try:
                as2utils.verify_payload(mdnContent,None,verify_cert)
            except Exception, e:
                try:
                    as2utils.verify_payload(mdnContent.split(main_boundary)[1].strip(),raw_sig,verify_cert)
                except Exception, e:
                    try:
                        as2utils.verify_payload(re.sub('\r\n\r\n$', '\r\n', mdnContent.split(main_boundary)[1].lstrip()),raw_sig,verify_cert)
                    except Exception, e:
                        raise as2exception("MDN Signature Verification Error, exception message is %s" %e)
        filename = messageId.strip('<>') + '.mdn'
        fullfilename = as2utils.join(init.gsettings['mdn_receive_store'], filename)
        file = open(fullfilename , 'wb')
        file.write(as2utils.extractpayload(mdnMessage))
        file.close()
        message.mdn = models.MDN.objects.create(message_id=messageId.strip('<>'),file=fullfilename, status='R', headers=mdnHeaders, signed=mdnsigned)
        if mdnMessage.get_content_type() == 'multipart/report':
            for part in mdnMessage.walk():
                if (part.get_content_type() == 'message/disposition-notification'):
                    models.Log.objects.create(message=message, status='S', text='Checking the MDN for status of the message')
                    mdn =  part.get_payload().pop()
                    mdnOMessageId = mdn.get('Original-Message-ID')
                    mdnStatus = mdn.get('Disposition').split(';')
                    if (mdnStatus[1].strip() == 'processed'):
                        models.Log.objects.create(message=message, status='S', text='Message has been successfully processed, verifying the MIC if present.')
                        if mdn.get('Received-Content-MIC'):
                            mdnMIC = mdn.get('Received-Content-MIC').split(',');
                            if (message.mic != mdnMIC[0]):
                                message.status = 'W'
                                mesage.adv_status = 'File transfer completed with warnings'
                                models.Log.objects.create(message=message, status='W', text='Message Integrity check failed, please validate message content with your partner')
                            else:
                                message.status = 'S'
                                message.adv_status = 'Completed'
                                models.Log.objects.create(message=message, status='S', text='File Transferred successfully to the partner')
                    else:
                        raise as2exception("Partner failed to process file. MDN status is %s"%(mdn.get('Disposition')))        
        else:
            raise as2exception("MDN message not found in the response")
    finally:
        message.save()

class as2exception(as2utils.AS2Error):
    pass

class as2duplicatedocument(as2utils.AS2Error):
    pass

class as2partnernotfound(as2utils.AS2Error):
    pass

class as2insufficientsecurity(as2utils.AS2Error):
    pass

class as2decompressionfailed(as2utils.AS2Error):
    pass

class as2decryptionfailed(as2utils.AS2Error):
    pass

class as2invalidsignature(as2utils.AS2Error):
    pass

