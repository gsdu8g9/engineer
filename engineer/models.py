# coding=utf-8
import logging
import re
from codecs import open
from copy import copy

import arrow
import yaml
from brownie.caching import cached_property
from dateutil import parser
from path import path
from propane.datastructures import CaseInsensitiveDict
from typogrify.filters import typogrify
from yaml.scanner import ScannerError

from engineer.conf import settings
from engineer.enums import Status
from engineer.exceptions import PostMetadataError
from engineer.filters import localtime
from engineer.plugins import PostProcessor, PostRenderer
from engineer.util import setonce, slugify, chunk, urljoin, wrap_list


try:
    # noinspection PyPep8Naming
    import cPickle as pickle
except ImportError:
    import pickle

__author__ = 'Tyler Butler <tyler@tylerbutler.com>'

logger = logging.getLogger(__name__)


class Post(object):
    """
    Represents a post written in Markdown and stored in a file.

    :param source: path to the source file for the post.
    """
    _regex = re.compile(
        r'^[\n|\r\n]*(?P<fence>---)?[\n|\r\n]*(?P<metadata>.+?)[\n|\r\n]*---[\n|\r\n]*(?P<content>.*)[\n|\r\n]*',
        re.DOTALL)

    # Make _content_raw only settable once. This is just to help prevent data loss that might be caused by
    # inadvertantly messing with this property.
    _content_raw = setonce()
    _file_contents_raw = setonce()

    # @staticmethod
    # def convert_to_html(content, renderer):
    #     return typogrify(renderer.render(content))

    @staticmethod
    def convert_post_to_html(post):
        return typogrify(post.content_renderer.render(post.content_preprocessed, post.format))

    def __init__(self, source):
        self.source = path(source).abspath()
        """The absolute path to the source file for the post."""

        self.html_template_path = 'theme/post_detail.html'
        """The path to the template to use to transform the post into HTML."""

        self.markdown_template_path = 'core/post.md'
        """The path to the template to use to transform the post back into a :ref:`post source file <posts>`."""

        # This will get set to `True in _parse_source if the source file has 'fenced metadata' (like Jekyll)
        self._fence = False

        metadata, self._content_raw = self._parse_source()

        if not hasattr(self, 'content_preprocessed'):
            self.content_preprocessed = self.content_raw

        self._content_finalized = self.content_raw

        # noinspection PyUnresolvedReferences
        # Handle any preprocessor plugins
        for plugin in PostProcessor.plugins:
            plugin.preprocess(self, metadata)

        self.title = metadata.pop('title', self.source.namebase.replace('-', ' ').replace('_', ' ').title())
        """The title of the post."""

        self.slug = metadata.pop('slug', slugify(self.title))
        """The slug for the post."""

        self._tags = wrap_list(metadata.pop('tags', []))

        self.link = metadata.pop('link', None)
        """The post's :ref:`external link <post link>`."""

        self.via = metadata.pop('via', None)
        """The post's attribution name."""

        self.via_link = metadata.pop('via-link', metadata.pop('via_link', None))
        """The post's attribution link."""

        try:
            self.status = Status(metadata.pop('status', Status.draft.name))
            """The status of the post (published or draft)."""

        except ValueError:
            logger.warning("'%s': Invalid status value in metadata. Defaulting to 'draft'." % self.title)
            self.status = Status.draft

        self.timestamp = metadata.pop('timestamp', None)
        """The date/time the post was published or written."""

        temp_timestamp = self.timestamp
        if temp_timestamp is None:
            temp_timestamp = arrow.now(settings.POST_TIMEZONE)
            # Reduce resolution of timestamp
            temp_timestamp = temp_timestamp.replace(second=0, microsecond=0)
        else:
            # looks like the timestamp from YAML wasn't directly convertible to a datetime, so we need to parse it
            timestamp_dt = parser.parse(str(temp_timestamp))
            if timestamp_dt.tzinfo is None:
                # convert to UTC assuming input time is in the POST_TIMEZONE
                temp_timestamp = arrow.get(timestamp_dt, settings.POST_TIMEZONE)
            else:
                temp_timestamp = arrow.get(timestamp_dt)

        self.timestamp = temp_timestamp.to('utc')

        self.content = Post.convert_post_to_html(self)
        """The post's content in HTML format."""

        # determine the URL based on the HOME_URL and the PERMALINK_STYLE settings
        permalink = settings.PERMALINK_STYLE.format(year=unicode(self.timestamp_local.year),
                                                    month=u'{0:02d}'.format(self.timestamp_local.month),
                                                    day=u'{0:02d}'.format(self.timestamp_local.day),
                                                    i_month=self.timestamp_local.month,
                                                    i_day=self.timestamp_local.day,
                                                    title=self.slug,  # for Jekyll compatibility
                                                    slug=self.slug,
                                                    timestamp=self.timestamp_local.datetime,
                                                    post=self)
        if permalink.endswith('index.html'):
            permalink = permalink[:-10]
        elif permalink.endswith('.html') or permalink.endswith('/'):
            pass
        else:
            permalink += '.html'
        self._permalink = permalink

        # keep track of any remaining properties in the post metadata
        metadata.pop('url', None)  # remove the url property from the metadata dict before copy
        self.custom_properties = copy(metadata)
        """A dict of any custom metadata properties specified in the post."""

        # noinspection PyUnresolvedReferences
        # handle any postprocessor plugins
        for plugin in PostProcessor.plugins:
            plugin.postprocess(self)

        # update cache
        settings.POST_CACHE[self.source] = self

    @cached_property
    def url(self):
        """The site-relative URL to the post."""
        url = u'{home_url}{permalink}'.format(home_url=settings.HOME_URL,
                                              permalink=self._permalink)
        url = re.sub(r'/{2,}', r'/', url)
        return url

    @cached_property
    def absolute_url(self):
        """The absolute URL to the post."""
        return u'{0}{1}'.format(settings.SITE_URL, self.url)

    @cached_property
    def output_path(self):
        url = self._permalink
        if url.endswith('/'):
            url += 'index.html'
        return path(settings.OUTPUT_CACHE_DIR / url)

    @cached_property
    def output_file_name(self):
        r = self.output_path.name
        return r

    @cached_property
    def tags(self):
        """A list of strings representing the tags applied to the post."""
        r = [unicode(t) for t in self._tags]
        return r

    @property
    def content_finalized(self):
        return self._content_finalized

    @property
    def content_raw(self):
        return self._content_raw

    @cached_property
    def content_renderer(self):
        return settings.POST_RENDERER_CONFIG[self.format]()

    @property
    def description(self):
        regex = re.compile(r'^.*?<p>(?P<para>.*?)</p>.*?', re.DOTALL)
        matches = re.match(regex, self.content)
        return matches.group('para')

    @property
    def format(self):
        return self.source.ext

    @property
    def is_draft(self):
        """``True`` if the post is a draft, ``False`` otherwise."""
        return self.status == Status.draft

    @property
    def is_published(self):
        """``True`` if the post is published, ``False`` otherwise."""
        return self.status == Status.published and self.timestamp <= arrow.now()

    @property
    def is_pending(self):
        """``True`` if the post is marked as published but has a timestamp set in the future."""
        return self.status == Status.published and self.timestamp >= arrow.now()

    @property
    def is_external_link(self):
        """``True`` if the post has an associated external link. ``False`` otherwise."""
        return self.link is not None and self.link != ''

    @property
    def timestamp_local(self):
        """
        The post's :attr:`timestamp` in 'local' time.

        Local time is determined by the :attr:`~engineer.conf.EngineerConfiguration.POST_TIMEZONE` setting.
        """
        return localtime(self.timestamp)

    def _parse_source(self):
        try:
            with open(self.source, mode='r') as the_file:
                item = unicode(the_file.read())
        except UnicodeDecodeError:
            with open(self.source, mode='r', encoding='UTF-8') as the_file:
                item = the_file.read()

        self._file_contents_raw = item
        parsed_content = re.match(self._regex, item)

        if parsed_content is None or parsed_content.group('metadata') is None:
            # Parsing failed, maybe there's no metadata
            raise PostMetadataError()

        if parsed_content.group('fence') is not None:
            self._fence = True

        # 'Clean' the YAML section since there might be tab characters
        metadata = parsed_content.group('metadata').replace('\t', '    ')
        try:
            metadata = yaml.load(metadata)
        except ScannerError:
            raise PostMetadataError("YAML error parsing metadata.")

        if not isinstance(metadata, dict):
            raise PostMetadataError("Metadata isn't a dict. Instead, it's a %s." % type(metadata))

        # Make the metadata dict case insensitive
        metadata = CaseInsensitiveDict(metadata)

        content = parsed_content.group('content')
        return metadata, content

    def render_html(self, all_posts=None):
        """
        Renders the Post as HTML using the template specified in :attr:`html_template_path`.

        :param all_posts: An optional :class:`PostCollection` containing all of the posts in the site.
        :return: The rendered HTML as a string.
        """
        index = all_posts.index(self)
        if index > 0:  # has newer posts
            newer_post = all_posts[index - 1]
        else:
            newer_post = None

        if index < len(all_posts) - 1:  # has older posts
            older_post = all_posts[index + 1]
        else:
            older_post = None
        return settings.JINJA_ENV.get_template(self.html_template_path).render(post=self,
                                                                               newer_post=newer_post,
                                                                               older_post=older_post,
                                                                               all_posts=all_posts,
                                                                               nav_context='post')

    def set_finalized_content(self, content, caller_class):
        """
        Plugins can call this method to modify post content that is written back to source post files.
        This method can be called at any time by anyone, but it has no effect if the caller is not granted the
        ``MODIFY_RAW_POST`` permission in the Engineer configuration.

        The :attr:`~engineer.conf.EngineerConfiguration.FINALIZE_METADATA` setting must also be enabled in order for
        calls to this method to have any effect.

        :param content: The modified post content that should be written back to the post source file.
        :param caller_class: The class of the plugin that's calling this method.
        :return: ``True`` if the content was successfully modified; otherwise ``False``.
        """
        caller = caller_class.get_name() if hasattr(caller_class, 'get_name') else unicode(caller_class)
        if not settings.FINALIZE_METADATA:
            logger.warning("A plugin is trying to modify the post content but the FINALIZE_METADATA setting is "
                           "disabled. This setting must be enabled for plugins to modify post content. "
                           "Plugin: %s" % caller)
            return False
        perms = settings.PLUGIN_PERMISSIONS['MODIFY_RAW_POST']
        if caller not in perms and '*' not in perms:
            logger.warning("A plugin is trying to modify the post content but does not have the "
                           "MODIFY_RAW_POST permission. Plugin: %s" % caller)
            return False
        else:
            logger.debug("%s is setting post source content." % caller)
            self._content_finalized = content
            return True

    def __unicode__(self):
        return self.slug

    __repr__ = __unicode__


class PostCollection(list):
    """A collection of :class:`Posts <engineer.models.Post>`."""

    # noinspection PyTypeChecker
    def __init__(self, seq=()):
        list.__init__(self, seq)
        self.listpage_template = settings.JINJA_ENV.get_template('theme/post_list.html')
        self.archive_template = settings.JINJA_ENV.get_template('theme/post_archives.html')

    def paginate(self, paginate_by=None):
        if paginate_by is None:
            paginate_by = settings.ROLLUP_PAGE_SIZE
        return chunk(self, paginate_by, PostCollection)

    @cached_property
    def published(self):
        """Returns a new PostCollection containing the subset of posts that are published."""
        return PostCollection([p for p in self if p.is_published is True])

    @cached_property
    def drafts(self):
        """Returns a new PostCollection containing the subset of posts that are drafts."""
        return PostCollection([p for p in self if p.is_draft is True])

    @property
    def pending(self):
        """Returns a new PostCollection containing the subset of posts that are pending."""
        return PostCollection([p for p in self if p.is_pending is True])

    @cached_property
    def review(self):
        """Returns a new PostCollection containing the subset of posts whose status is :attr:`~Status.review`."""
        return PostCollection([p for p in self if p.status == Status.review])

    @cached_property
    def all_tags(self):
        """Returns a list of all the unique tags, as strings, that posts in the collection have."""
        tags = set()
        for post in self:
            tags.update(post.tags)
        return list(tags)

    def tagged(self, tag):
        """Returns a new PostCollection containing the subset of posts that are tagged with *tag*."""
        return PostCollection([p for p in self if unicode(tag) in p.tags])

    @staticmethod
    def output_path(slice_num):
        return path(settings.OUTPUT_CACHE_DIR / ("page/%s/index.html" % slice_num))

    def render_listpage_html(self, slice_num, has_next, has_previous, all_posts=None):
        return self.listpage_template.render(
            post_list=self,
            slice_num=slice_num,
            has_next=has_next,
            has_previous=has_previous,
            all_posts=all_posts,
            nav_context='listpage')

    def render_archive_html(self, all_posts=None):
        return self.archive_template.render(post_list=self,
                                            all_posts=all_posts,
                                            nav_context='archive')

    def render_tag_html(self, tag, all_posts=None):
        return settings.JINJA_ENV.get_template('theme/tags_list.html').render(tag=tag,
                                                                              post_list=self.tagged(tag),
                                                                              all_posts=all_posts,
                                                                              nav_context='tag')


class TemplatePage(object):
    def __init__(self, template_path):
        self.html_template = settings.JINJA_ENV.get_template(
            str(settings.TEMPLATE_DIR.relpathto(template_path)).replace('\\', '/'))
        namebase = template_path.namebase
        name_components = settings.TEMPLATE_PAGE_DIR.relpathto(template_path).splitall()[1:]
        name_components[-1] = namebase
        self.name = '/'.join(name_components)
        self.absolute_url = urljoin(settings.HOME_URL, self.name)
        self.output_path = path(settings.OUTPUT_CACHE_DIR / self.name)
        self.output_file_name = 'index.html'

        settings.URLS[self.name] = self.absolute_url

    def render_html(self, all_posts=None):
        rendered = self.html_template.render(nav_context=self.name,
                                             all_posts=all_posts)
        return rendered
