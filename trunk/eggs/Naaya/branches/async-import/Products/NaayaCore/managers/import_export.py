""" Bulk upload contacts, urls, experts """

from AccessControl.SecurityManagement import getSecurityManager
from zope.site.hooks import getSite, setSite
from AccessControl import ClassSecurityInfo, Unauthorized
from Acquisition import Implicit
from DateTime import DateTime
from Globals import InitializeClass
from OFS.SimpleItem import Item
from Products.Five.browser import BrowserView
from Products.NaayaBase.NyContentType import NyContentData
from Products.NaayaBase.constants import PERMISSION_PUBLISH_OBJECTS
from Products.NaayaCore.GeoMapTool.managers import geocoding
from Products.NaayaCore.SchemaTool.widgets.GeoWidget import GeoWidget
from Products.NaayaCore.SchemaTool.widgets.geo import Geo
from Products.NaayaCore.events import CSVImportEvent
from Products.NaayaCore.interfaces import ICSVImportExtraColumns
from Products.PageTemplates.PageTemplateFile import PageTemplateFile
from persistent import Persistent
from StringIO import StringIO
from ZPublisher.BaseRequest import BaseRequest
from ZPublisher.Response import Response
from itertools import tee
from naaya.content.base.events import NyContentObjectEditEvent
from naaya.core.async import Job
from naaya.core.ggeocoding import GeocoderServiceError
from persistent.list import PersistentList
from random import randint
from zc.async.interfaces import KEY as ZCASYNC_KEY
from zc.async.queue import Queue
from zc.async.job import Job as ZCAsyncJob
from zope.event import notify
import csv, codecs
import logging
import simplejson as json
import time
import transaction
import urllib
import xlrd
import xlwt


logger = logging.getLogger(__name__)


class CSVImporterTask(Persistent):
    """ An import task for rows from Excel/CSV files
    """

    payload  = None
    template = None
    error    = None
    status   = 'unfinished'

    def __init__(self, *args, **kwargs):
        super(CSVImporterTask, self).__init__(*args, **kwargs)
        self.warnings = PersistentList()
        self.errors = PersistentList()

    def on_failure(self, *args, **kwargs):
        self.status = 'failed'

    def on_success(self, *args, **kwargs):
        self.status = 'finished'

    def run(self, *args, **kwargs):

        self.payload = kwargs['payload']
        self.template = kwargs['template']
        self.rec_id = kwargs['rec_id']

        site = getSite()

        user = getSecurityManager().getUser()
        acl_users = site.acl_users
        user = user.__of__(acl_users)
        user_id = kwargs['user_id']

        request = BaseRequest()
        request.response = Response()
        request.AUTHENTICATED_USER = user
        request['PARENTS'] = [site]
        request['URL'] = kwargs['url']
        request.steps = []
        request.cookies = {}
        request.form = {}

        import_location = site.unrestrictedTraverse(kwargs['import_path'])

        import_location.REQUEST = request
        site.REQUEST = request

        geo_fields = kwargs['geo_fields']
        self.prop_map = kwargs['properties']
        meta_type = kwargs['meta_type']

        header = self.template
        row = self.payload
        record_number = self.rec_id

        content_type = import_location.getSite().get_pluggable_item(meta_type)
        add_object = content_type['add_method']

        properties = {}
        extra_properties = {}
        address = None

        for column, value in zip(header, row):

            if value == '':
                continue

            if column not in self.prop_map:
                extra_properties[column] = value
                continue

            key = self.prop_map[column]['column']
            widget = self.prop_map[column]['widget']
            widget = widget.__of__(import_location)
            convert = widget.convert_from_user_string
            properties[key] = convert(value)

        try:
            properties = do_geocoding(geo_fields, properties)
        except GeocoderServiceError, e:
            msg = ('Warnings: could not find a valid address '
                    'for row ${record_number}: ${error}',
                    {'record_number': record_number + 1,     # account for header
                    'error': str(e)})
            self.warnings.append(msg)
            address = properties.pop(geo_fields['address'])

        ob_id = add_object(import_location, _send_notifications=False,
                                **properties)
        ob = import_location._getOb(ob_id)
        if address:
            setattr(ob, geo_fields['address'].split('.')[0],
                    Geo(address=address))
            notify(NyContentObjectEditEvent(ob, user_id))
        if extra_properties:
            adapter = ICSVImportExtraColumns(ob, None)
            if adapter is not None:
                extra_props_messages = adapter.handle_columns(
                    extra_properties)
                if extra_props_messages:
                    self.errors.append(extra_props_messages)
        #obj_ids.append(ob.getId())
        ob.submitThis()
        ob.approveThis(_send_notifications=False)

        del import_location.REQUEST
        del site.REQUEST

    # except UnicodeDecodeError, e:
    #     raise
    # except Exception, e:
    #     self.log_current_error()
    #     msg = ('Error while importing from file, row ${record_number}: ${error}',
    #             {'record_number': record_number + 1,     # account for header
    #             'error': str(e)})
    #     if REQUEST is None:
    #         raise ValueError(msg)
    #     else:
    #         errors.append(msg)

    #     except UnicodeDecodeError, e:
    #         if REQUEST is None:
    #             raise
    #         else:
    #             errors.append('CSV file is not utf-8 encoded')


def do_geocoding(geo_fields, properties):
    time.sleep(1)
    lat = properties.get(geo_fields['lat'], '')
    lon = properties.get(geo_fields['lon'], '')
    address = properties.get(geo_fields['address'], '')

    if lat.strip() == '' and lon.strip() == '' and address:
        coordinates = geocoding.geocode(None, address)
        print "Got coordinated", coordinates, address
        if coordinates != None:
            lat, lon = coordinates
            properties[geo_fields['lat']] = lat
            properties[geo_fields['lon']] = lon

    return properties


class CSVImportTool(Implicit, Item):
    title = "Import from structured file"

    security = ClassSecurityInfo()
    geo_fields = {}

    def __init__(self, id):
        self.id = id

    security.declareProtected(PERMISSION_PUBLISH_OBJECTS, 'template')
    def template(self, meta_type, file_type, as_attachment=False, REQUEST=None):
        """ """
        if REQUEST and not self.getParentNode().checkPermissionPublishObjects():
            raise Unauthorized

        schema = self.getSite().getSchemaTool().getSchemaForMetatype(meta_type)
        if schema is None:
            raise ValueError('Schema for meta-type "%s" not found' % meta_type)
        columns = []
        for widget in schema.listWidgets():
            if widget.multiple_form_values:
                for subname in widget.multiple_form_values:
                    columns.append(widget.title + ' - ' + subname)
            else:
                columns.append(widget.title)

        if file_type == 'CSV':
            ret = generate_csv(columns, [[]])
            content_type = 'text/csv; charset=utf-8'
            filename = schema.title_or_id() + ' bulk upload.csv'

        elif file_type == 'Excel':
            ret = generate_excel(columns, [[]])
            content_type = 'application/vnd.ms-excel'
            filename = schema.title_or_id() + ' bulk upload.xls'

        else: raise ValueError('unknown file format %r' % file_type)

        if REQUEST is not None:
            set_response_attachment(REQUEST.RESPONSE, filename,
                content_type, len(ret))

        return ret

    def parse_data(self, data, file_type, REQUEST=None):
        errors = []
        if file_type == 'Excel':
            try:
                wb = xlrd.open_workbook(file_contents=data.read())
                ws = wb.sheets()[0]
                header = ws.row_values(0)
                rows = []
                for i in range(ws.nrows)[1:]:
                    rows.append(ws.row_values(i))
            except xlrd.XLRDError:
                msg = 'Invalid Excel file'
                if REQUEST is None:
                    raise ValueError(msg)
                else:
                    errors.append(msg)
                    rows = []
        elif file_type == 'CSV':
            rows = UnicodeReader(data)
            try:
                header = rows.next()
            except StopIteration:
                msg = 'Invalid CSV file'
                if REQUEST is None:
                    raise ValueError(msg)
                else:
                    errors.append(msg)
                    rows = []
            except UnicodeDecodeError:
                if REQUEST is None:
                    raise
                else:
                    errors.append('CSV file is not utf-8 encoded')
        else:
            raise ValueError('unknown file format %r' % file_type)

        return (rows, header, errors)

    security.declareProtected(PERMISSION_PUBLISH_OBJECTS, 'do_import')
    def do_import(self, meta_type, file_type, data, REQUEST=None):
        """ """
        if REQUEST and not self.getParentNode().checkPermissionPublishObjects():
            raise Unauthorized

        errors = []
        warnings = []

        schema = self.getSite().getSchemaTool().getSchemaForMetatype(meta_type)
        if schema is None:
            raise ValueError('Schema for meta-type not found: "%s"' % meta_type)

        location_obj = self.getParentNode()

        # build a list of property names based on the object schema
        # TODO: extract this loop into a separate function
        prop_map = {}
        for widget in schema.listWidgets():
            prop_name = widget.prop_name()

            if widget.multiple_form_values:
                for subname in widget.multiple_form_values:
                    prop_subname = prop_name + '.' + subname
                    prop_map[widget.title + ' - ' + subname] = {
                        'column': prop_subname,
                        'widget':widget,
                    }
                if isinstance(widget, GeoWidget):
                    for subname in widget.multiple_form_values:
                        self.geo_fields[subname] = prop_name + '.' + subname
            else:
                prop_map[widget.title] = {
                    'column': prop_name,
                    'widget':widget,
                }

        rows, header, _errors = self.parse_data(data, file_type)
        errors.extend(_errors)

        if (REQUEST is not None) and errors:
            # transaction.abort()
            self.setSessionErrorsTrans(errors)
            return self.index_html(REQUEST, meta_type=meta_type)

        #     else:
        #         if warnings:
        #             self.setSessionErrorsTrans(warnings)
        #         self.setSessionInfoTrans('${records} object(s) of type "${title}" successfully imported.',
        #         records=record_number, title=schema.title_or_id())

        record_number = 0
        user_id = location_obj.REQUEST.AUTHENTICATED_USER.id

        queue, queue_id = make_queue(self)
        for row in rows[:20]:
            record_number += 1
            task = CSVImporterTask()

            options = {'context':location_obj,
                       'import_path':'/'.join(location_obj.getPhysicalPath()),
                       'meta_type':meta_type,
                       'user_id':user_id,
                       'properties':prop_map,
                       'payload':row,
                       'template':header,
                       'rec_id':record_number,
                       'geo_fields':self.geo_fields.copy(),
                       'site':location_obj.getSite(),
                       'url':location_obj.REQUEST.URL,
                       }

            queue.tasks.append(task)
            transaction.savepoint()
            job = Job(task.run, **options)
            job.addCallbacks(task.on_success, task.on_failure)
            queue.put(job)

        return self.async_import_html(REQUEST, queue_id=queue_id)

        # TODO: put back this
        # if not errors:
        #     notify(CSVImportEvent(location_obj, obj_ids))

        # if REQUEST is not None:
        #     if errors:
        #         transaction.abort()
        #         self.setSessionErrorsTrans(errors)
        #     else:
        #         if warnings:
        #             self.setSessionErrorsTrans(warnings)
        #         self.setSessionInfoTrans('${records} object(s) of type "${title}" successfully imported.',
        #         records=record_number, title=schema.title_or_id())

        #     return self.index_html(REQUEST, meta_type=meta_type)


    security.declareProtected(PERMISSION_PUBLISH_OBJECTS, 'index_html')
    index_html = PageTemplateFile('../zpt/bulk_import', globals())
    async_import_html = PageTemplateFile('../zpt/async_bulk_import', globals())
    csv_specifications = PageTemplateFile('../zpt/csv_specifications', globals())

InitializeClass(CSVImportTool)


class ExportTool(Implicit, Item):
    title = "Spreadsheet export"
    CELL_CHAR_LIMIT = 32000

    security = ClassSecurityInfo()

    def __init__(self, id):
        self.id = id

    def _dump_objects(self, meta_type, objects):
        """
        Returns a tuple. First value is a list of column names; second
        value is an iterable, which yields one list of string values for
        each dumped object.

        The column names and object values are conveniently arranged for
        exporting as a table (e.g. CSV file)
        """
        schema = self.getSite().getSchemaTool().getSchemaForMetatype(meta_type)
        if schema is None:
            raise ValueError('Schema for meta-type "%s" not found' % meta_type)

        def getter_factory(prop_name, subname, convert, default=u''):
            def getter(ob):
                try:
                    ob_property = getattr(ob.aq_base, prop_name)

                    if subname is None:
                        value = ob_property
                    else:
                        value = getattr(ob_property, subname)

                except AttributeError:
                    return default

                else:
                    return convert(value)

            return getter

        def simple_convert(value):
            if value is None:
                return u''
            else:
                return unicode(value)

        prop_getters = []
        dump_header = []

        # create columns for schema widgets
        for widget in schema.listWidgets():
            prop_name = widget.prop_name()
            if widget.multiple_form_values:
                for subname in widget.multiple_form_values:
                    dump_header.append(widget.title + ' - ' + subname)
                    getter = getter_factory(prop_name, subname, simple_convert)
                    prop_getters.append(getter)
            else:
                dump_header.append(widget.title)
                convert = widget.convert_to_user_string
                getter = getter_factory(prop_name, None, convert)
                prop_getters.append(getter)

        def generate_dump_items():
            for ob in objects:
                item = [unicode(get_value(ob)) for get_value in prop_getters]
                yield item

        dump_items = generate_dump_items()

        return dump_header, dump_items

    security.declarePrivate('generate_csv_output')
    def generate_csv_output(self, meta_type, objects):
        dump_header, dump_items = self._dump_objects(meta_type, objects)

        return generate_csv(dump_header, dump_items)

    security.declarePrivate('generate_excel_output')
    def generate_excel_output(self, meta_type, objects):
        dump_header, dump_items = self._dump_objects(meta_type, objects)

        def validate_column_size(header, rows_generator):
            rows, rows_backup = tee(rows_generator)
            try:
                for item in rows:
                    for col in range(len(item)):
                        if len(item[col]) >= self.CELL_CHAR_LIMIT:
                            org = item[0]
                            object_type =  meta_type.replace('Naaya ', '', 1)
                            oversized_col = header[col]
                            error_msg = ("Unable to export to Excel file."
                                         "There is a %s (%s) with "
                                         "a column (%s) exceeding the Excel "
                                         "cell size limit"
                                         % (object_type, org, oversized_col))
                            raise ValueError(error_msg.encode('utf-8'))
            except Exception, error:
                return error
            else:
                return generate_excel(header, rows_backup)

        return validate_column_size(dump_header, dump_items)

    security.declareProtected(PERMISSION_PUBLISH_OBJECTS, 'export')
    def export(self, meta_type, file_type="CSV", as_attachment=False, REQUEST=None):
        """ """
        if REQUEST and not self.getParentNode().checkPermissionPublishObjects():
            raise Unauthorized

        search = self.getSite().getCatalogedObjects
        objects = search(meta_type=[meta_type],
                         path='/'.join(self.aq_parent.getPhysicalPath()))

        if file_type == 'CSV':
            ret = self.generate_csv_output(meta_type, objects)
            content_type = 'text/csv; charset=utf-8'
            filename = '%s Export.csv' % meta_type

        elif file_type == 'Excel':
            ret = self.generate_excel_output(meta_type, objects)
            content_type = 'application/vnd.ms-excel'
            filename = '%s Export.xls' % meta_type

        else: raise ValueError('unknown file format %r' % file_type)

        if isinstance(ret, ValueError):
            self.setSessionErrors(ret)
            return self.REQUEST.RESPONSE.redirect(self.REQUEST.HTTP_REFERER)

        if as_attachment and REQUEST is not None:
            filesize = len(ret)
            set_response_attachment(REQUEST.RESPONSE, filename,
                content_type, filesize)

        return ret

    security.declareProtected(PERMISSION_PUBLISH_OBJECTS, 'export_json')
    def export_json(self, meta_type=None, pretty=False,
            as_attachment=False, REQUEST=None):
        """
        export all objects of `meta_type` (or, if None, all content) as JSON
        """

        if as_attachment and REQUEST is not None:
            set_response_attachment(REQUEST.RESPONSE,
                '%s.json' % self.getSite().getId(), 'application/json')
        data = {}
        for ob in self.getSite().getCatalogedObjects():
            if not isinstance(ob, NyContentData):
                continue
            ob_data = dict(ob.dump_data(), meta_type=ob.meta_type)
            data[relative_path_to_site(ob)] = ob_data

        if pretty:
            kwargs = {'indent': 4}
        else:
            kwargs = {'sort_keys': False}

        return json.dumps(data, default=json_encode, **kwargs)

    security.declareProtected(PERMISSION_PUBLISH_OBJECTS, 'index_html')
    index_html = PageTemplateFile('../zpt/bulk_export', globals())

InitializeClass(ExportTool)

def json_encode(ob):
    """ try to encode some known value types to JSON """
    if isinstance(ob, DateTime):
        return str(ob)
    raise ValueError

def attachment_header(filename):
    assert isinstance(filename, str)
    try:
        filename.decode('ascii')
    except UnicodeDecodeError:
        value = "filename*=UTF-8''%s" % urllib.quote(filename)
    else:
        value = "filename=%s" % urllib.quote(filename)
    return "attachment; " + value

def set_response_attachment(RESPONSE, filename, content_type, length=None):
    RESPONSE.setHeader('Content-Type', content_type)
    if length is not None:
        RESPONSE.setHeader('Content-Length', length)
    RESPONSE.setHeader('Pragma', 'public')
    RESPONSE.setHeader('Cache-Control', 'max-age=0')
    RESPONSE.setHeader('Content-Disposition', attachment_header(filename))

def relative_path_to_site(ob):
    site = ob.getSite()
    site_path = '/'.join(site.getPhysicalPath())
    ob_path = '/'.join(ob.getPhysicalPath())
    return ob_path[len(site_path):]


class UTF8Recoder(object):
    """
    Iterator that reads an encoded stream and reencodes the input to UTF-8
    """
    def __init__(self, f, encoding):
        self.reader = codecs.getreader(encoding)(f)

    def __iter__(self):
        return self

    def next(self):
        return self.reader.next().encode("utf-8")


class UnicodeReader(object):
    """
    A CSV reader which will iterate over lines in the CSV file "f",
    which is encoded in the given encoding.
    """

    def __init__(self, f, dialect=csv.excel, encoding="utf-8", **kwds):
        f = UTF8Recoder(f, encoding)
        self.reader = csv.reader(f, dialect=dialect, **kwds)

    def next(self):
        row = self.reader.next()
        return [unicode(s, "utf-8") for s in row]

    def __iter__(self):
        return self


class CSVReader(object):
    """ Manipulate CSV files """

    def __init__(self, file, dialect, encoding):
        """ """
        if dialect == 'comma':
            dialect=csv.excel
        elif dialect == 'tab':
            dialect=csv.excel_tab
        else:
            dialect=csv.excel
        self.csv = UnicodeReader(file, dialect, encoding)

    def read(self):
        """ return the content of the file """
        try:
            header = self.csv.next()
            output = []
            for values in self.csv:
                buf = {}
                for field, value in zip(header, values):
                    buf[field.encode('utf-8')] = value.encode('utf-8')
                output.append(buf)
            return (output, '')
        except Exception, ex:
            logger.exception('Read error')
            return (None, ex)

def generate_csv(header, rows):

    output = StringIO()
    csv_writer = csv.writer(output)

    csv_writer.writerow(header)
    for item in rows:
        csv_writer.writerow([value.encode('utf-8') for value in item])

    return output.getvalue()

def generate_excel(header, rows):
    style = xlwt.XFStyle()
    normalfont = xlwt.Font()
    headerfont = xlwt.Font()
    headerfont.bold = True
    style.font = headerfont

    wb = xlwt.Workbook(encoding='utf-8')
    ws = wb.add_sheet('Sheet 1')
    row = 0
    for col in range(0, len(header)):
        ws.row(row).set_cell_text(col, header[col], style)
    style.font = normalfont
    for item in rows:
        row += 1
        for col in range(0, len(item)):
            ws.row(row).set_cell_text(col, item[col], style)
    output = StringIO()

    wb.save(output)
    return output.getvalue()


def get_queue(context, queue_id):
    queues = context._p_jar.root()[ZCASYNC_KEY]
    return queues[queue_id]


def get_queue_info(context, queue_id):
    queue = get_queue(context, queue_id)

    done = []
    error = []
    unfinished = []

    for job in queue.get_jobs():
        if job.status == 'done':
            done.append(job)
        elif job.status == 'error':
            error.append(job)
        elif job.status == 'unfinished':
            unfinished.append(job)

    info = {
        'stats':{'total':len(done + error + unfinished),
                 'unfinished':len(unfinished),
                 'done':len(done),
                 'error':len(error),
                 },
        'log_done':[j.status_text for j in done],
        'log_errors':[j.status_text for j in error]
    }

    return info

def make_queue(context):
    root = context.getSite()._p_jar.root()

    queues = root[ZCASYNC_KEY]
    queue = Queue()
    id = str(randint(0, 999999))
    queues[id] = queue

    queue.tasks = PersistentList()
    queue._p_changed = True
    transaction.savepoint() # this bind the queue to the Connection
    #queue.imspecial = True

    return (queue, id)


class QueueInfo(BrowserView):
    """ A debug view to show results about queues
    """

    def __call__(self):
        queue_id = self.request.form.get('queue_id')
        queue = get_queue(self.context, queue_id)

        return get_queue_broken_rows(queue)


class RemoveAllActiveJobs(BrowserView):

    def __call__(self):
        queues = self.context.getSite()._p_jar.root()[ZCASYNC_KEY]
        ids = [i for i in queues.keys() if i != '']     # avoid the default queue
        for k in ids:
            del queues[k]

        return "Deleted all"


def get_queue_broken_rows(queue):
    header = None
    rows = []

    for job in queue.jobs:
        if job.status == 'completed-status':
            rows.append(job.payload)
            if not header:
                header = job.header

    #return ChangeToExcel(rows, header)


class MakeAJob(BrowserView):
    """ A simple page to make a simple job
    """

    def __call__(self):

        request = BaseRequest()
        site = self.context.getSite()
        request['PARENTS'] = [site]
        request.steps = list(self.context.getPhysicalPath())
        request['URL'] = site.absolute_url()
        request.response = Response()
        import pdb; pdb.set_trace()
        request.traverse('who-who')

        return
        # import Zope2

        # app = Zope2.app()
        # root = app._p_jar.root()
        # print ZCASYNC_KEY in root
