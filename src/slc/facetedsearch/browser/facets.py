from copy import deepcopy
from logging import getLogger

from DateTime import DateTime
from ZTUtils import make_hidden_input

from zope import component 
from zope.schema.interfaces import IVocabularyFactory

from Products.Archetypes.interfaces import IVocabulary
from Products.CMFCore.utils import getToolByName
from Products.Five import BrowserView
from Products.Five.browser.pagetemplatefile import ViewPageTemplateFile

from collective.solr.browser import facets 
from collective.solr.interfaces import ISolrConnectionManager

log = getLogger(__name__)

DATE_LOWERBOUND = '1000-01-01T00:00:00Z'
DATE_UPPERBOUND = '2499-12-31T23:59:59Z'

RANGE_TYPES = ['float', 'long', 'double', 'date']

def facetParameters(context, request):
    """ determine facet fields to be queried for """
    marker = []
    fields, dependencies = facets.facetParameters(context, request)
    ranges = request.get('facet.range', request.get('facet_range', marker))
    if isinstance(ranges, basestring):
        ranges = [ranges]
    if ranges is marker:
        ranges = getattr(context, 'facet_ranges', marker)
    if ranges is marker:
        ranges = []
        
    if fields is None:
        fields = []

    mgr = component.getUtility(ISolrConnectionManager)
    schema = mgr.getSchema()
    for field in fields:
        if schema[field]['type'] in RANGE_TYPES:
            fields.remove(field)
            if field not in ranges:
                ranges.append(field)

    types = dict()
    for f in fields:
        types.update({f:'standard'})
    for r in ranges:
        types.update({r:'range'})

    return dict(fields=tuple(fields) + tuple(ranges), 
                types=types, 
                dependencies=dependencies)


class FacetMixin:
    """ mixin with helpers common to the viewlet and view """
    hidden = ViewPageTemplateFile('templates/hiddenfields.pt')

    def hiddenfields(self):
        """ render hidden fields suitable for inclusion in search forms """
        facet_params = facetParameters(self.context, self.request)
        queries = facets.param(self, 'fq')
        fields = filter(lambda f: facet_params['types'][f] == 'standard', facet_params['fields'])
        ranges = filter(lambda f: facet_params['types'][f] == 'range', facet_params['fields'])
        return self.hidden(facets=fields, ranges=ranges, queries=queries, other=[])


class SearchFacetsView(BrowserView, FacetMixin):
    """ view for displaying facetting info as provided by solr searches """

    def __init__(self, context, request):
        pdict = facetParameters(context, request)
        self.facet_fields = pdict['fields']
        self.facet_types = pdict['types']
        self.facet_range_gap = 7 # days

        self.friendly_type_names = \
                    component.queryUtility(
                        IVocabularyFactory, 
                        name=u'plone.app.vocabularies.ReallyUserFriendlyTypes')(self)

        standard = filter(lambda f: self.facet_types[f] == 'standard', self.facet_fields)
        ranges = filter(lambda f: self.facet_types[f] == 'range', self.facet_fields)
        self.default_query = {'facet': 'true',
                              'facet.field': standard,
                              'b_size': 10}
        if ranges:
            # TODO: need config for these values
            self.default_query.update({
                              'facet.range': ranges, 
                              'facet.range.start': 'NOW/DAY-6MONTHS',
                              'facet.range.end': 'NOW/DAY', 
                              'facet.range.gap': '+%sDAYS' % self.facet_range_gap,
                              'facet.range.other': 'all', })

        self.submenus = [dict(title=field, id=field) for field in self.facet_fields]
        self.queryparam = 'fq'
        BrowserView.__init__(self, context, request)


    def __call__(self):
        self.form = deepcopy(self.default_query)
        self.form.update(deepcopy(self.request.form))

        catalog = getToolByName(self.context, 'portal_catalog')
        query = deepcopy(self.form)
        self.results = catalog(query)

        facet_counts = getattr(self.results, 'facet_counts', {})
        voctool = getToolByName(self.context, 'portal_vocabularies', None)
        self.vocDict = dict()

        for field in self.facet_fields:
            voc = {}
            if voctool:
                voc = voctool.getVocabularyByName(field)
            if IVocabulary.providedBy(voc):
                self.vocDict[field] = ( voc.Title(), 
                                        voc.getVocabularyDict(self.context))
            elif facet_counts: 
                # we don't have a matching vocabulary, so we fake one
                before = after = -1
                if field in facet_counts['facet_fields']:
                    field_values = facet_counts['facet_fields'][field].keys()

                elif field in facet_counts['facet_ranges']:
                    field_values= facet_counts['facet_ranges'][field]['counts'].keys()
                    before = facet_counts['facet_ranges'][field].get('before', -1)
                    after = facet_counts['facet_ranges'][field].get('after', -1)
                else:
                    continue

                content = dict()
                if before >= 0:
                    content[DATE_LOWERBOUND] = ('Before', None)
                if after >= 0:
                    content[DATE_UPPERBOUND] = ('After', None)

                for value in field_values:
                    content[value] = (self.getFriendlyValue(field, value), None)

                self.vocDict[field] = (self.getFriendlyFieldName(field), content)

    def getFriendlyTypeName(self, typename):
        try:
            return self.friendly_type_names.getTermByToken(typename).title
        except LookupError:
            return typename

    def getFriendlyFieldName(self, fieldname):
        atct = getToolByName(self.context, 'portal_atct')
        if fieldname in atct.getIndexes():
            return atct.getIndex(fieldname).friendlyName
        else:
            return fieldname

    def getFriendlyValue(self, field, value):
        solr_conn = component.getUtility(ISolrConnectionManager)
        if solr_conn.getSchema()[field]['type'] == 'date':
            return DateTime(value).strftime('%d.%m.%Y')
        return value

    def getCounts(self):
        res = self.results
        if not hasattr(res, 'facet_counts'):
            return {}
        counts = res.facet_counts['facet_fields']
        for rng in res.facet_counts['facet_ranges']:
            counts[rng] = res.facet_counts['facet_ranges'][rng]['counts']
        return counts

    def sort(self, submenu):
        return sorted(submenu, key=lambda x:x['count'], reverse=True)

    def sortrange(self, submenu):
        return sorted(submenu, key=lambda x:x['id'], reverse=False)

    def getMenu(self, 
                id='ROOT', 
                title=None, 
                vocab={}, 
                counts=None, 
                parent=None, 
                facettype=None, 
                sortkey=None):
        menu = []
        if not vocab and id == 'ROOT':
            vocab = self.vocDict
        if not counts and id == 'ROOT':
            counts = self.getCounts()

        count = 0
        if isinstance(counts, int):
            count = int(counts)

        if not facettype or id in self.facet_fields:
            facettype = self.facet_types.get(id, 'standard')
        isrange = facettype == 'range'
        isstandard = facettype == 'standard'

        if vocab:
            for term in vocab:
                submenu = []
                counts_sub = None
                if isinstance(counts, dict):
                    counts_sub = counts.get(term, None)
                title_sub = ''
                vocab_sub = vocab.get(term, {})
                if vocab_sub:
                    title_sub = vocab_sub[0]
                    vocab_sub = vocab_sub[1]
                submenu = self.getMenu(
                                    id=term, 
                                    title=title_sub, 
                                    vocab=vocab_sub, 
                                    counts=counts_sub, 
                                    parent=id, 
                                    facettype=facettype)
                menu.append(submenu)
            if sortkey:
                menu = sorted(menu, key=sortkey)
            else:
                if isrange:
                    menu = self.sortrange(menu)
                else:
                    menu = self.sort(menu)

            if isrange and not filter(lambda item: item['selected_from'], menu):
                menu[0]['selected_from'] = True
            if isrange and not filter(lambda item: item['selected_to'], menu):
                menu[-1]['selected_to'] = True

        selected = False
        selected_from = False
        selected_to = False
        form = getattr(self.request, 'form', {})
        if parent not in [None, 'ROOT']:
            if isrange:
                value = form.get(parent, {})
                if isinstance(value, list):
                    if len(value) == 1:
                        date = DateTime(value[0])
                        range = 'min' # or maybe max?
                    elif len(value) == 2:
                        date = map(DateTime, value)
                        range = 'min:max' # or maybe max?
                    else: # got more than 2 values or none, fall back to sth sensible
                        date = [DATE_LOWERBOUND, DATE_UPPERBOUND]
                        range = 'min:max'
                elif hasattr(value, 'get'):
                    date = map(DateTime, value.get('query', []))
                    range = form.get(parent, {}).get('range')
                else:
                    date = DateTime(value)
                    range = 'min' # see above

                if range == 'min':
                    selected = DateTime(id) - date < 7 and DateTime(id) - date >= 0
                    selected_from = selected
                    selected_to = False
                    date = date.HTML4()
                elif range == 'max':
                    selected = DateTime(id) - date < 7 and DateTime(id) - date >= 0
                    selected_to = selected
                    selected_from = False
                    date = date.HTML4()
                elif range == 'min:max' and date:
                    # XXX: doublecheck this
                    selected_from = DateTime(id) - date[0] < 7 and DateTime(id) - date[0] >= 0
                    selected_to = DateTime(id) - date[1] < 7 and DateTime(id) - date[1] >= 0
                    selected = selected_from or selected_to
            else:
                queried = form.get(parent)
                if queried == id:
                    selected = True
                elif isinstance(queried, (list, tuple)) and id in queried:
                    selected  = True

        return dict(id=id, 
                    title=title, 
                    isrange=isrange, 
                    isstandard=isstandard, 
                    selected=selected, 
                    selected_from=selected_from, 
                    selected_to=selected_to, 
                    count=count, 
                    content=menu)

    def showSubmenu(self, submenu):
        """Returns True if submenu has an entry with query or clearquery set,
            i.e. should be displayed
        """
        return not filter(lambda x: x.get('selected', False) \
                          or x['count']>0, submenu) == []

    def expandSubmenu(self, submenu):
        """Returns True if submenu has an entry with clearquery set, i.e.
           should be displayed expanded
        """
        return not filter(lambda x: x.has_key('clearquery'), submenu) == []

    def getHiddenFields(self):
        return make_hidden_input([x for x in self.form.items() if x[0] not in self.facet_fields and not 'facet' in x[0] and not '_usage' in x[0]])

