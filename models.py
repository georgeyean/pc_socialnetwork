# -*- coding: utf-8 -*- 
from __future__ import print_function, unicode_literals, absolute_import, division
import logging, datetime, string
import random
from django.db import models
from django.conf import settings
from django.contrib import admin
from django.contrib.sites.models import Site

from django.utils.timezone import utc
from datetime import timedelta
from biostar.apps.util import now
from django.utils.translation import ugettext_lazy as _
from django.core.urlresolvers import reverse
import bleach
from django.db.models import Q, F
from django.core.exceptions import ObjectDoesNotExist
from biostar import const
from biostar.apps.users.models import User, UserAdminLog
from biostar.apps.util import html
from biostar.apps import util
from django.core.paginator import Paginator
from lxml.html.clean import clean_html, Cleaner
# HTML sanitization parameters.

logger = logging.getLogger(__name__)

def now():
    return datetime.datetime.utcnow().replace(tzinfo=utc)

class Tag(models.Model):
    name = models.TextField(max_length=50, db_index=True)
    count = models.IntegerField(default=0)
    watch_count = models.IntegerField(default=0)

    @staticmethod
    def fixcase(name):
        return name.upper() if len(name) == 1 else name.lower()

    @staticmethod
    def update_counts(action, post):
        "Applies tag count updates upon post changes"
        for tag in post.tag_set.all():
            count = Post.objects.filter(type__in=Post.TOP_LEVEL, tag_set__id__in=[tag.id], status=Post.OPEN)\
            .count()
            tag.count = count
            tag.save()

        # if action == 'post_add':
        #     post.tag_set.all().update(count=F('count') + 1)

        # if action == 'post_remove':
        #     post.tag_set.all().update(count=F('count') - 1)
        #     #Tag.objects.filter(pk__in=pk_set).update(count=F('count') - 1)

        # #if action == 'pre_clear':
        #     #instance.tag_set.all().update(count=F('count') - 1)

    @staticmethod
    def top_tags(count):
        
        tags = Tag.objects.all().order_by('-count')[:count]
        return random.sample(tags, 3)


    def __unicode__(self):
        return self.name

class TagAdmin(admin.ModelAdmin):
    list_display = ('name', 'count')
    search_fields = ['name']


admin.site.register(Tag, TagAdmin)

class PostManager(models.Manager):

    def my_bookmarks(self, user):
        query = self.filter(votes__author=user, votes__type=Vote.BOOKMARK)
        query = query.select_related("root", "author", "lastedit_user")
        query = query.prefetch_related("tag_set")
        return query

    def my_posts(self, target, user):

        # Show all posts for moderators or targets
        if user.is_moderator or user == target:
            query = self.filter(author=target)
        else:
            query = self.filter(author=target).exclude(status=Post.DELETED)

        query = query.select_related("root", "author", "lastedit_user")
        query = query.prefetch_related("tag_set")
        query = query.order_by("-creation_date")
        return query

    def fixcase(self, text):
        return text.upper() if len(text) == 1 else text.lower()

    def update_user_posts_count_all(self, user, type):
        if type == Post.COMMENT:
            cmt_cnt = self.filter(author=user, type=Post.COMMENT, status=Post.OPEN).count()
            user.cnt_cmt=cmt_cnt            
        
        if type == Post.QUESTION:
            q_cnt = self.filter(author=user, type=Post.QUESTION, status=Post.OPEN).count()
            user.cnt_question=q_cnt
        
        if type == Post.ANSWER:
            a_cnt = self.filter(author=user, type=Post.ANSWER, status=Post.OPEN).count()
            user.cnt_answer=a_cnt
                
        user.save()

    def update_post_count_all(self, post, type):
        if type == Post.COMMENT:
            mainpost = post
            cmt_cnt = self.filter(mainpost_id=mainpost.id, type=Post.COMMENT, status__in=(Post.OPEN, Post.HIDDEN))\
            .count()
            print(cmt_cnt)
            mainpost.comment_count=cmt_cnt   
            mainpost.save()         
        
        if type == Post.ANSWER:
            root = post
            a_cnt = self.filter(root_id=root.id, type=Post.ANSWER, status__in=(Post.OPEN, Post.HIDDEN)).count()
            root.real_reply_count=a_cnt
            print(a_cnt)
            root.save()
                

    def get_posts_from_tagid(self, tag_id, count=0):
        max_count = 20
        query = self.filter(type__in=Post.TOP_LEVEL, tag_set__id__in=[tag_id], status=Post.OPEN)
        if count: # 探索
            max = query.count()
            #get top 20 posts for this tag
            query = query.values("id", "title").order_by('-real_reply_count')[:20]
            #get random a few from top 20
            query = random.sample(query, count if max>count else max)
        else:
            query = query.values("id", "title").order_by('-real_reply_count')[:max_count]
        return query

    def parse_tags(self):
        return util.split_tags(self.tag_val)

    def get_tags_from_str(self, text):
        def parse_tags(tag_val):
            return util.split_tags(tag_val)
        text = text.strip()
        if not text:
            return []
        texts = parse_tags(text)
        tags = Tag.objects.filter(name__in=texts).values('id', 'name', 'count', 'watch_count')
        return tags

    def tag_search(self, text, user=None):
        "Performs a query by one or more , separated tags"
        #text = "经济 历史 读书"
        include, exclude = [], []
        # Split the given tags on ',' and '+'.
        terms = text.split(',') if ',' in text else text.split(' ')
        for term in terms:
            term = term.strip()
            if term.endswith("!"):
                exclude.append(self.fixcase(term[:-1]))
            else:
                include.append(self.fixcase(term))

        if include:
            query = self.filter(type__in=Post.TOP_LEVEL, tag_set__name__in=include).exclude(type=Post.BLOG).exclude(
                tag_set__name__in=exclude)
        else:
            query = self.filter(type__in=Post.TOP_LEVEL).exclude(type=Post.BLOG).exclude(tag_set__name__in=exclude)


        if user and user.is_authenticated() and user.is_moderator:
            pass
        else:
            query = query.filter(status__in=[Post.OPEN, Post.TOOPEN])
        # Get the tags.
        query = query.select_related("author",  "author__profile")\
        .prefetch_related("tag_set").distinct().order_by('-lastedit_date')
        # Remove fields that are not used.
        query = query.defer()

        return query

    def get_thread(self, root, user, page=1, inverse=0):
        # Populate the object to build a tree that contains all posts in the thread.
        is_moderator = user and user.is_authenticated() and user.is_moderator

        if is_moderator:
            if inverse:
                query = self.filter(root=root, type=Post.ANSWER).exclude(status__in=(Post.HIDDEN, Post.DRAFT))\
                .select_related("root", "author", "author__profile")\
                .defer('content')\
                .order_by("-creation_date")
            else:
                query = self.filter(root=root, type=Post.ANSWER).exclude(status__in=(Post.HIDDEN, Post.DRAFT))\
                .select_related("root", "author", "author__profile")\
                .defer('content')\
                .order_by("type", "-has_accepted", "-vote_count", "creation_date")
        else:  #George: not display cmt
            if inverse:
                query = self.filter(root=root, type=Post.ANSWER, status=Post.OPEN)\
                .select_related("root", "author", "author__profile")\
                .defer('content')\
                .order_by("-creation_date")
            else:
                query = self.filter(root=root, type=Post.ANSWER, status=Post.OPEN)\
                .select_related("root", "author", "author__profile")\
                .defer('content')\
                .order_by("type", "-has_accepted", "-vote_count", "creation_date")
        # import pdb
        # pdb.set_trace()
        paginator = Paginator(query, 15)
    #.exclude(type=Post.COMMENT)
        return paginator.page(page)

    def get_thread_hide(self, root, user, page=1):
        # Populate the object to build a tree that contains all posts in the thread.
        is_moderator = user and  user.is_authenticated() and user.is_moderator

        if is_moderator:
            query = self.filter(root=root, type=Post.ANSWER, status=Post.HIDDEN)\
            .select_related("root", "author", "author__profile", "lastedit_user")\
            .defer('content')\
            .order_by("type", "-has_accepted", "-vote_count", "creation_date")
        else:  #George: not display cmt
            query = self.filter(root=root, type=Post.ANSWER, status=Post.HIDDEN)\
            .select_related("root", "author", "author__profile", "lastedit_user")\
            .defer('content')\
            .order_by("type", "-has_accepted", "-vote_count", "creation_date")
        paginator = Paginator(query, 100)
    #.exclude(type=Post.COMMENT)
        return paginator.page(page)

    def get_comments(self, pid, user):
        # Populate the object to build a tree that contains all posts in the thread.
        is_moderator = user.is_authenticated() and user.is_moderator
        if is_moderator:
            query = self.filter(type=Post.COMMENT, mainpost_id=pid, status=Post.OPEN).select_related\
            ( "author", "author__profile")\
            .defer("mainpost__html", "parent__html")\
            .order_by("creation_date")
        else:  #George: not display cmt
            query = self.filter(type=Post.COMMENT, mainpost_id=pid, status=Post.OPEN)\
            .select_related("parent", "parent__author", "author", "author__profile", "mainpost__author", "parent_author", "lastedit_user").order_by("creation_date")
    #.exclude(type=Post.COMMENT)
        return query

    def get_comments_hidden(self, pid, user):
        # Populate the object to build a tree that contains all posts in the thread.
        is_moderator = user.is_authenticated() and user.is_moderator
        if is_moderator:
            query = self.filter(type=Post.COMMENT, mainpost_id=pid, status=Post.HIDDEN).select_related\
            ( "author", "author__profile")\
            .defer("mainpost__html", "parent__html")\
            .order_by("creation_date")
        else:  #George: not display cmt
            query = self.filter(type=Post.COMMENT, mainpost_id=pid, status=Post.HIDDEN)\
            .select_related("parent", "parent__author", "author", "author__profile", "mainpost__author", "parent_author", "lastedit_user").order_by("creation_date")
    #.exclude(type=Post.COMMENT)
        return query

    def top_level(self, user, defer_large_context=False):
        "Returns posts based on a user type"
        if user:
            is_moderator = user.is_authenticated() and user.is_moderator
            if is_moderator:
                query = self.filter(type__in=Post.TOP_LEVEL, status=Post.OPEN, isfront=True).exclude(type__in=[Post.BLOG, Post.NEWS])
            else:
                query = self.filter(type__in=Post.TOP_LEVEL, status=Post.OPEN, isfront=True).exclude(type__in=[Post.BLOG, Post.NEWS])
        else:
            query = self.filter(type__in=Post.TOP_LEVEL, status=Post.OPEN, isfront=True).exclude(type__in=[Post.BLOG, Post.NEWS])
        
        #here we can jump to get profile table 
        query = query.select_related("author", "author__profile").prefetch_related("tag_set")
        if defer_large_context: 
            return query.defer("content")
        else:
            return query

    def top_level_ids(self, user):
        "Returns questions (with content of top answer) based on a user type"
        if user:
            is_moderator = user.is_authenticated() and user.is_moderator
            if is_moderator:
                query = self.filter(type__in=Post.TOP_LEVEL, status=Post.OPEN, isfront=True).exclude(type__in=[Post.BLOG, Post.NEWS])
            else:
                query = self.filter(type__in=Post.TOP_LEVEL, status=Post.OPEN, isfront=True).exclude(type__in=[Post.BLOG, Post.NEWS])
        else:
            query = self.filter(type__in=Post.TOP_LEVEL, status=Post.OPEN, isfront=True).exclude(type__in=[Post.BLOG, Post.NEWS])

        query = query.values("id")
        return query

    def pop_daily(self, user):
        "Returns questions pop in daily(by answer count)"
        if user:
            is_moderator = user.is_authenticated() and user.is_moderator
            if is_moderator:
                query = self.filter(type__in=[Post.QUESTION, Post.BLOG], status__in=[Post.OPEN, Post.TOOPEN])
            else:
                query = self.filter(type__in=[Post.QUESTION, Post.BLOG], status__in=[Post.OPEN, Post.TOOPEN])
        else:
            query = self.filter(type__in=[Post.QUESTION, Post.BLOG], status__in=[Post.OPEN, Post.TOOPEN])

        date_from = datetime.datetime.now() - datetime.timedelta(days=3)
        query = query.filter(creation_date__gte=date_from)
        query = query.order_by("-real_reply_count")
        query = query.values("id")
        return query


    def pop_monthly(self, user):
        "Returns questions pop in monthly(by answer count)"
        if user:
            is_moderator = user.is_authenticated() and user.is_moderator
            if is_moderator:
                query = self.filter(type__in=[Post.QUESTION, Post.BLOG], status__in=[Post.OPEN, Post.TOOPEN])
            else:
                query = self.filter(type__in=[Post.QUESTION, Post.BLOG], status__in=[Post.OPEN, Post.TOOPEN])
        else:
            query = self.filter(type__in=[Post.QUESTION, Post.BLOG], status__in=[Post.OPEN, Post.TOOPEN])

        date_from = datetime.datetime.now() - datetime.timedelta(days=30)
        query = query.filter(creation_date__gte=date_from)
        query = query.order_by("-real_reply_count")
        query = query.values("id")
        return query

    def top_answer(self, pid):
        "get the highest score answer. return None if no answer"
        ans = None
        question = self.filter(id=pid)\
        .select_related("root", "root__real_reply_count", "root__subs_count", "author", "author__profile", \
            "author__profile__info", "lastedit_user")\
        .prefetch_related("tag_set").order_by('-score')[0]
        if question.real_reply_count>0:
            anss = self.filter(root_id=pid, type=Post.ANSWER, status=Post.OPEN)\
            .select_related("root", "root__real_reply_count", "root__subs_count",  "author", "author__profile",\
            "author__profile__info", "lastedit_user")\
        .prefetch_related("tag_set").order_by('-score')
            from random import randint
            if len(anss)==0:
                ans = question
            elif len(anss)==1:
                ans = anss[0]
            elif len(anss)==2:                
                ans = anss[randint(0, 1)]
            #elif len(anss)==3:                
            else:
                ans = anss[randint(0, 2)]
            #elif len(anss)==4:                
            #    ans = anss[randint(0, 3)]
            #else:
            #    ans = anss[randint(0, 4)]
        elif question.real_reply_count == 0:
            ans = question
        return ans

    def index_answers(self, pid):
        query = self.filter(type__in=[Post.ANSWER], status__in=[Post.OPEN], score__gte=10).order_by('-creation_date')
        return query


    def get_subscription(self, posts, user, use_root=0):
        votes = []
        pids = []
        if user.is_authenticated():
            if use_root:
                pids = [p.root_id for p in posts]
            else:
                pids = [p.id for p in posts]
            subs = Subscription.objects.filter(post_id__in=pids, user=user)
        else:
            return posts

        def decorate(post):
            for sub in subs:
                if use_root:
                    if sub.post_id == post.root_id:
                        post.sub = sub 
                else:
                    if sub.post_id == post.id:
                        post.sub = sub

        # Add attributes by mutating the objects
        map(decorate, posts)
        return posts

    def get_votes_status(self, posts, user):

        store = {Vote.UP: set(), Vote.DOWN: set(),Vote.BOOKMARK: set()}

        if user.is_authenticated():
            pids = [p.id for p in posts]
            votes = Vote.objects.filter(post_id__in=pids, author=user).values_list("post_id", "type")

            for post_id, vote_type in votes:
                store.setdefault(vote_type, set()).add(post_id)
        else:
            return posts

        # Shortcuts to each storage.
        bookmarks = store[Vote.BOOKMARK]
        upvotes = store[Vote.UP]
        downvotes = store[Vote.DOWN]

        def decorate(post):
            post.has_bookmark = post.id in bookmarks
            post.has_upvote = post.id in upvotes
            post.has_downvote = post.id in downvotes
            post.can_accept = (post.author_id == user.id) or post.has_accepted

        # Add attributes by mutating the objects
        map(decorate, posts)
        return posts

    def get_vote_status_feed(self, feeds, ups, downs, books, user):

        if user.is_authenticated():
            pids = [f.feed.obj_id for f in feeds]
            votes = Vote.objects.filter(post_id__in=pids, author=user).values_list("post_id", "type")

            for post_id, vote_type in votes:
                if vote_type == Vote.BOOKMARK:
                    books.append(post_id)
                if vote_type == Vote.UP:
                    ups.append(post_id)
                if vote_type == Vote.DOWN:
                    downs.append(post_id)
        return None


    def get_peeks(self, posts, length):

        def decorate(post):
            post.peek = post.peek(length)
            post.as_text_len = len(post.as_text)
        map(decorate, posts)
        return posts

    def add_mytags_sub(self, my_tags, tag):
        mytags_set = util.split_tags(my_tags)
        tag['has_mytags_sub'] = False
        if tag['name'] in mytags_set:
            tag['has_mytags_sub'] = True
        return tag

    def add_wishes(self, user, posts):

        store = {"i": set(), "a": set()}
        if user.is_authenticated():
            pids = [p.id for p in posts]
            interests = Wish.objects.filter(post_id__in=pids, user=user, type=Wish.INTEREST).values("post_id")
            answers = Wish.objects.filter(post_id__in=pids, user=user, type=Wish.ANSWER).values("post_id")
            for i in interests:
                store['i'].add(i['post_id'])
            for a in answers:
                store['a'].add(a['post_id'])
        else:
            return posts

        def decorate(post):
            post.wish_interest = 1 if post.id in store['i'] else 0
            post.wish_answer = 1 if post.id in store['a'] else 0

        # Add attributes by mutating the objects
        map(decorate, posts)
        return posts

def test1():
    logger.info("test1")

class Post(models.Model):
    "Represents a post in Biostar"

    objects = PostManager()

    # Post statuses.
    PENDING, OPEN, CLOSED, DELETED, HIDDEN, DRAFT, HOT, TOOPEN = range(8)
    STATUS_CHOICES = [(PENDING, "审核中"), (OPEN, "正常"), (CLOSED, "已关闭"), (DELETED, "已删除"),
                    (HIDDEN, "已隐藏"), (DRAFT, "草稿"), (HOT, "热点"), (TOOPEN, "问题池")]

    #hide_reason
    VIEW, SIMPLE, EMOTION, MAKEUP = range(4)
    HIDE_REASON_CHOICES = [(VIEW, "恶意灌水等"), (SIMPLE, "粗浅或低质量"), (EMOTION, "过于情绪化"), (MAKEUP, "明显编造")]
    HIDE_REASON_CHOICES_CMT = [(VIEW, "恶意灌水等"), (EMOTION, "过于情绪化")]
    # Question types. Answers should be listed before comments.
    QUESTION, ANSWER, JOB, FORUM, PAGE, BLOG, COMMENT, DATA, TUTORIAL, BOARD, TOOL, NEWS, IDEA = range(13)

    TYPE_CHOICES = [
        (QUESTION, "Question"), (ANSWER, "Answer"), (COMMENT, "Comment"), (IDEA, "Idea"),
        (JOB, "Job"), (FORUM, "Forum"), (TUTORIAL, "Tutorial"),
        (DATA, "Data"), (PAGE, "Page"), (TOOL, "Tool"), (NEWS, "News"),
        (BLOG, "Blog"), (BOARD, "Bulletin Board")
    ]

    TOP_LEVEL = set((QUESTION, JOB, FORUM, PAGE, BLOG, DATA, TUTORIAL, TOOL, BOARD, IDEA, NEWS))

    title = models.CharField(max_length=200, null=False)

    # The user that originally created the post.
    author = models.ForeignKey(settings.AUTH_USER_MODEL)

    # The user that edited the post most recently.
    lastedit_user = models.ForeignKey(settings.AUTH_USER_MODEL, related_name='editor')

    # Indicates the information value of the post.
    rank = models.FloatField(default=0, blank=True)

    # Indicates the information value of the post.
    score = models.FloatField(default=0, blank=True)

    # Post status: open, closed, deleted.
    status = models.IntegerField(choices=STATUS_CHOICES, default=OPEN)

    # The type of the post: question, answer, comment.
    type = models.IntegerField(choices=TYPE_CHOICES, db_index=True)

    # Number of upvotes for the post
    vote_count = models.IntegerField(default=0, blank=True, db_index=True)

    # Number of upvotes for the post
    downvote_count = models.IntegerField(default=0, blank=True, db_index=True)

    # The number of views for the post.
    view_count = models.IntegerField(default=0, blank=True)

    # The number of replies that a post has.
    reply_count = models.IntegerField(default=0, blank=True)

    # The number of replies that a post has.
    real_reply_count = models.IntegerField(default=0, blank=True)

    # The number of comments that a post has.
    comment_count = models.IntegerField(default=0, blank=True)

    # Bookmark count.
    book_count = models.IntegerField(default=0)

    # Indicates indexing is needed.
    changed = models.BooleanField(default=True)

    # How many people follow that thread.
    subs_count = models.IntegerField(default=0)

    # How many people show interest on thread.
    wish_interest_count = models.IntegerField(default=0)

    # How many people want to answer thread.
    wish_answer_count = models.IntegerField(default=0)

    # The total score of the thread (used for top level only)
    thread_score = models.IntegerField(default=0, blank=True, db_index=True)

    hide_reason = models.IntegerField(choices=HIDE_REASON_CHOICES, default=0)

    # Date related fields.
    creation_date = models.DateTimeField(db_index=True)
    lastedit_date = models.DateTimeField(db_index=True)

    # Stickiness of the post.
    sticky = models.BooleanField(default=False, db_index=True)

    isfront = models.BooleanField(default=False, db_index=True)

    # Indicates whether the post has accepted answer.
    has_accepted = models.BooleanField(default=False, blank=True)

    # This will maintain the ancestor/descendant relationship bewteen posts.
    root = models.ForeignKey('self', related_name="descendants", null=True, blank=True, on_delete=models.SET_NULL)
    root_authorid = models.IntegerField(default=0)
    root_authorname = models.CharField(max_length=30)

    # This will maintain parent/child replationships between posts.
    parent = models.ForeignKey('self', null=True, blank=True, related_name='children', on_delete=models.SET_NULL)
    parent_authorid = models.IntegerField(default=0)
    parent_authorname = models.CharField(max_length=30)

    # This will maintain mainpost/child replationships between posts. mainpost is first level question/answer, no cmts
    mainpost = models.ForeignKey('self', null=True, blank=True, related_name='sub_comments', on_delete=models.SET_NULL)
    mainpost_authorid = models.IntegerField(default=0)
    mainpost_authorname = models.CharField(max_length=30)
    # This is the HTML that the user enters.
    content = models.TextField(default='')

    # This is the  HTML that gets displayed.
    html = models.TextField(default='')

    attachimg = models.TextField(default='')

    # The tag value is the canonical form of the post's tags
    tag_val = models.CharField(max_length=100, default="", blank=True)

    # The tag set is built from the tag string and used only for fast filtering
    tag_set = models.ManyToManyField(Tag, blank=True, null=True)

    # What site does the post belong to.
    site = models.ForeignKey(Site, null=True)

    def parse_tags(self):
        return util.split_tags(self.tag_val)

    def add_tags(self, text):
        text = text.strip()
        if not text:
            return
        # Sanitize the tag value
        self.tag_val = bleach.clean(text, tags=[], attributes=[], styles={}, strip=True)
        # Clear old tags
        self.tag_set.clear()
        tags = [Tag.objects.get_or_create(name=name)[0] for name in self.parse_tags()]
        self.tag_set.add(*tags)
        #self.save()

    def get_tags_from_str(self, text):
        text = text.strip()
        if not text:
            return []
        tags = [Tag.objects.get_or_create(name=name)[0] for name in self.parse_tags()]
        return tags

    @property
    def as_text(self):
        "Returns the body of the post after stripping the HTML tags"
        text = bleach.clean(self.content, tags=[], attributes=[], styles={}, strip=True)
        return text

    def peek(self, length=100):
        "A short peek at the post"
        return self.as_text[:length]

    def get_title(self):
        if self.status == Post.OPEN:
            return self.get_translated_title()
        else:
            return "%s" % (self.get_translated_title())

    def get_translated_title(self):

        if self.type == Post.QUESTION or self.type == Post.BLOG:
            return self.title
        elif self.type == Post.ANSWER:
            return self.title.replace('A:', '回答了: ')
        elif self.type == Post.COMMENT:
            return self.title.replace('C:', '评论了: ')
        else:
            return self.title 

    def get_root_title(self):

        if self.type == Post.QUESTION or self.type == Post.BLOG:
            return self.title
        elif self.type == Post.ANSWER:
            return self.title.replace('A:', '')
        elif self.type == Post.COMMENT:
            return self.title.replace('C:', '')
        else:
            return self.title 

    @property
    def is_open(self):
        return self.status == Post.OPEN or self.status == Post.TOOPEN

    @property
    def is_pending(self):
        return self.status == Post.PENDING

    @property
    def is_closed(self):
        return self.status == Post.CLOSED

    @property
    def age_in_days(self):
        delta = const.now() - self.creation_date
        return delta.days

    def update_reply_count(self, delta):
        "This can be used to set the answer count."
        if self.type == Post.ANSWER:
            #reply_count = Post.objects.filter(parent=self.parent, type=Post.ANSWER, status=Post.OPEN).count()
            real_reply_count = self.parent.real_reply_count
            if delta < 0 and real_reply_count == 0:
                return
            Post.objects.filter(pk=self.parent_id).update(real_reply_count=real_reply_count+delta)

    def update_comment_count(self, delta):
        "This can be used to set the comment count."
        if self.type == Post.COMMENT:
            comment_count = self.mainpost.comment_count
            if delta < 0 and comment_count == 0:
                return
            Post.objects.filter(pk=self.mainpost_id).update(comment_count=comment_count+delta)
 
    


    def delete(self, using=None):
        # Collect tag names.
        tag_names = [t.name for t in self.tag_set.all()]

        # While there is a signal to do this it is much faster this way.
        Tag.objects.filter(name__in=tag_names).update(count=F('count') - 1)

        if self.status != Post.DRAFT:
            if self.type == Post.ANSWER:
                self.update_reply_count(-1)
            elif self.type == Post.COMMENT:
                self.update_comment_count(-1)

        # Remove tags with zero counts.
        #Tag.objects.filter(count=0).delete()
        super(Post, self).delete(using=using)

    def fake_delete(self, using=None):
        # Collect tag names.
        tag_names = [t.name for t in self.tag_set.all()]

        # While there is a signal to do this it is much faster this way.
        Tag.objects.filter(name__in=tag_names).update(count=F('count') - 1)

        if self.status != Post.DRAFT:
            if self.type == Post.ANSWER:
                self.update_reply_count(-1)
            elif self.type == Post.COMMENT:
                self.update_comment_count(-1)

        # Remove tags with zero counts.
        #Tag.objects.filter(count=0).delete()
        Post.objects.filter(id=self.id).update(status=Post.DELETED)

    def save(self, update=False, *args, **kwargs):
        #import pdb
        #pdb.set_trace() why this function is called twice??????
        # Sanitize the post body.
        self.html = html.parse_html(self.content)

        # Must add tags with instance method. This is just for safety.
        self.tag_val = html.strip_tags(self.tag_val)
        #print(self.html)
        cleanerRich = Cleaner(
                  links=False, embedded=False,
                  #scripts=True, javascript=True, embedded=True, meta=True, page_structure=True, links=True, style=True, annoying_tags=True,
                  # inline_style=True, forms=True,
                  remove_unknown_tags=False,
                  allow_tags=['p','blockquote','ul','li','h4','b','i','u','strike','sup','ol','a','img','iframe','br','hr','table','tbody', 'tr',
                  'td', 'div'])
        self.html = cleanerRich.clean_html(self.html)

        if self.type == self.COMMENT:
            #print(self.html)
            #self.html = '<div class="stream-item-header"><a class="account-group js-account-group js-action-profile js-user-profile-link js-nav" rel="nofollow" href="https://twitter.com/PingSereanRyan"><span class="FullNameGroup"><strong class="fullname show-popup-with-id u-textTruncate ">s11111</div>'
            cleaner = Cleaner(                     
                      #scripts=True, javascript=True, embedded=True, meta=True, page_structure=True, links=True, style=True, annoying_tags=True,
                      # inline_style=True, forms=True,
                      remove_unknown_tags=False,
                      remove_tags = ['img', 'li', 'td', 'h1', 'h2','h3','h4', 'strong', 'button','a'],
                      allow_tags=['p','br', 'div'])
            self.html = cleaner.clean_html(self.html)
            #print(self.html)


        # Posts other than a question also carry the same tag
        if self.is_toplevel and self.type != Post.QUESTION and self.type != Post.IDEA and self.type != Post.BLOG and self.type != Post.NEWS:
            required_tag = self.get_type_display()
            if required_tag not in self.tag_val:
                self.tag_val += "," + required_tag

        if not self.id:

            # Set the titles
            if self.parent and not self.title:
                self.title = self.parent.title

            if self.parent and self.parent.type in (Post.ANSWER, Post.COMMENT):
                # Only comments may be added to a parent that is answer or comment.
                self.type = Post.COMMENT

            if self.type is None:
                # Set post type if it was left empty.
                self.type = self.COMMENT if self.parent else self.FORUM

            # This runs only once upon object creation.
            self.title = self.parent.title if self.parent else self.title
            self.lastedit_user = self.author
            self.status = self.status or Post.PENDING
            self.creation_date = self.creation_date or now()
            self.lastedit_date = self.creation_date


        # Recompute post reply count
        if update == True:     
            if self.type == Post.ANSWER:
                #self.update_reply_count(1) #put this outside, as  answer is more complex
                pass
            elif self.type == Post.COMMENT:
                self.update_comment_count(1)


        super(Post, self).save( *args, **kwargs)

    def __unicode__(self):
        return "%s: %s (id=%s)" % (self.get_type_display(), self.title, self.id)

    @property
    def is_toplevel(self):
        return self.type in Post.TOP_LEVEL

    def get_absolute_url(self):
        "A blog will redirect to the original post"
        #if self.url:
        #    return self.url
        url = "/p/%d"%self.root_id#reverse("post-details", kwargs=dict(pk=self.root_id))
        if self.type == Post.COMMENT:
            if self.mainpost_id:
                url = "%s?s=%s&c=%s#cmt_area_%s" % (url, self.mainpost_id, self.id, self.mainpost_id)
            else: 
                url = "%s?s=%s&c=%s#cmt_area_%s" % (url, self.id, self.id, self.id)
        elif self.type == Post.ANSWER:
            url = url if self.is_toplevel else "%s?s=%s" % (url, self.id)
        
        return url

    def get_root_url(self):
        "A blog will redirect to the original post"
        #if self.url:
        #    return self.url
        url = reverse("post-details", kwargs=dict(pk=self.root_id))
        return url

    @staticmethod
    def update_post_views(post, request, minutes=settings.POST_VIEW_MINUTES):
        "Views are updated per user session"

        # Extract the IP number from the request.
        ip1 = request.META.get('REMOTE_ADDR', '')
        ip2 = request.META.get('HTTP_X_FORWARDED_FOR', '').split(",")[0].strip()
        # 'localhost' is not a valid ip address.
        ip1 = '' if ip1.lower() == 'localhost' else ip1
        ip2 = '' if ip2.lower() == 'localhost' else ip2
        ip = ip1 or ip2 or '0.0.0.0'

        now = const.now()
        since = now - datetime.timedelta(minutes=minutes)

        # One view per time interval from each IP address.
        if not PostView.objects.filter(ip=ip, post=post, date__gt=since):
            PostView.objects.create(ip=ip, post=post, date=now)
            Post.objects.filter(id=post.id).update(view_count=F('view_count') + 1)
        return post

    @staticmethod
    def check_root(sender, instance, created, *args, **kwargs):
        "We need to ensure that the parent and root are set on object creation."
        if created:

            if not (instance.root or instance.parent):
                # Neither root or parent are set.
                instance.root = instance.parent = instance

            elif instance.parent:
                # When only the parent is set the root must follow the parent root.
                instance.root = instance.parent.root

            elif instance.root:
                # The root should never be set on creation.
                raise Exception('Root may not be set on creation')

            if instance.parent.type in (Post.ANSWER, Post.COMMENT):
                # Answers and comments may only have comments associated with them.
                instance.type = Post.COMMENT

            assert instance.root and instance.parent

            if not instance.is_toplevel:
                # Title is inherited from top level.
                instance.title = "%s: %s" % (instance.get_type_display()[0], instance.root.title[:80])

                if instance.type == Post.ANSWER:
                    Post.objects.filter(id=instance.root.id).update(reply_count=F("reply_count") + 1)

            instance.save()


class ReplyToken(models.Model):
    """
    Connects a user and a post to a unique token. Sending back the token identifies
    both the user and the post that they are replying to.
    """
    user = models.ForeignKey(settings.AUTH_USER_MODEL)
    post = models.ForeignKey(Post)
    token = models.CharField(max_length=256)
    date = models.DateTimeField(auto_created=True)

    def save(self, *args, **kwargs):
        if not self.id:
            self.token = util.make_uuid()
        super(ReplyToken, self).save(*args, **kwargs)

class ReplyTokenAdmin(admin.ModelAdmin):
    list_display = ('user', 'post', 'token', 'date')
    ordering = ['-date']
    search_fields = ('post__title', 'user__name')

admin.site.register(ReplyToken, ReplyTokenAdmin)

class PostAdminLog(models.Model):
    """
    Connects a user and a post to a unique token. Sending back the token identifies
    both the user and the post that they are replying to.
    """
    doee = models.ForeignKey(settings.AUTH_USER_MODEL, related_name='doee')
    post = models.ForeignKey(Post)
    doer = models.ForeignKey(settings.AUTH_USER_MODEL, related_name='doer')
    type = models.IntegerField(db_index=True)
    comment = models.TextField(default='')
    date = models.DateTimeField(auto_created=True)

class Subsvpnemail(models.Model):
    email = models.TextField(default='')
    date = models.DateTimeField(auto_created=True)

class SubsvpnemailAdmin(admin.ModelAdmin):
    list_display = ('email', 'date')
    ordering = ['-date']

admin.site.register(Subsvpnemail, SubsvpnemailAdmin)


class Answerinvite(models.Model):
    doer = models.ForeignKey(settings.AUTH_USER_MODEL, related_name='inviter')
    post = models.ForeignKey(Post)
    doee = models.ForeignKey(settings.AUTH_USER_MODEL, related_name='invitee')
    date = models.DateTimeField(auto_created=True)

class AnswerinviteAdmin(admin.ModelAdmin):
    list_display = ('doer','doee', 'post',  'date')
    ordering = ['-date']

admin.site.register(Answerinvite, AnswerinviteAdmin)


class Feed(models.Model):
    QUESTION, ANSWER, IDEA = range(3)
    FEED_TYPES = [
        (QUESTION, "关注话题有了新问题"), (ANSWER, "关注用户有了新回答"),
        (IDEA, "发布了动态"),
    ]
    content = models.TextField(default='')
    type = models.IntegerField(choices=FEED_TYPES, db_index=True)
    obj_id = models.IntegerField(default=0)
    send_id = models.IntegerField(default=0)
    status = models.IntegerField(default=0)#0 show, 1 hide
    date = models.DateTimeField(auto_created=True)

    def save(self, *args, **kwargs):
        "Actions that need to be performed on every  save."
        super(Feed, self).save(*args, **kwargs) 

class FeedAdmin(admin.ModelAdmin):
    list_display = ('content', 'type', 'date')
    ordering = ['-date']

admin.site.register(Feed, FeedAdmin)

class FeedPush(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL)
    feed = models.ForeignKey(Feed)
    date = models.DateTimeField(auto_created=True)

class FeedPushAdmin(admin.ModelAdmin):
    list_display = ('user', 'feed', 'date')
    ordering = ['-date']
    
admin.site.register(FeedPush, FeedPushAdmin)

class Report(models.Model):
    """
    user reports a post
    """
    #1. 信口雌黄，毫无依据
    #2. 过度情绪化表达  
    #3. 单纯表达立场 (政治话题相关)
    #4. 
    NOFACT, EMOTION, VIEWONLY = range(3)
    REPORT_TYPES = [
        (NOFACT, "信口雌黄，毫无依据"), (EMOTION, "过度情绪化表达"),
        (VIEWONLY, "广告恶意灌水等"),
    ]
    user = models.ForeignKey(settings.AUTH_USER_MODEL)
    post = models.ForeignKey(Post)
    type = models.IntegerField(choices=REPORT_TYPES, db_index=True)
    date = models.DateTimeField(auto_created=True)
    #comment by admin
    comment = models.TextField(default='')


class ReportAdmin(admin.ModelAdmin):
    list_display = ('user', 'post', 'type', 'date')
    ordering = ['-date']
    search_fields = ('post__title', 'user__name')

admin.site.register(Report, ReportAdmin)

class Wish(models.Model):
    """
    user reports a post
    """
    #1. 信口雌黄，毫无依据
    #2. 过度情绪化表达  
    #3. 单纯表达立场 (政治话题相关)
    #4. 
    INTEREST, ANSWER = range(2)
    WISH_TYPES = [
        (INTEREST, "感兴趣"), (ANSWER, "想回答"), 
    ]
    user = models.ForeignKey(settings.AUTH_USER_MODEL)
    post = models.ForeignKey(Post)
    type = models.IntegerField(choices=WISH_TYPES, db_index=True)
    date = models.DateTimeField(auto_created=True)
    #comment by admin
    comment = models.TextField(default='')

class WishAdmin(admin.ModelAdmin):
    list_display = ('user', 'post', 'type', 'date')
    ordering = ['-date']
    search_fields = ('post__title', 'user__name')

admin.site.register(Wish, WishAdmin)

class EmailSub(models.Model):
    """
    Represents an email subscription to the digest digest.
    """
    SUBSCRIBED, UNSUBSCRIBED = 0, 1
    TYPE_CHOICES = [
        (SUBSCRIBED, "Subscribed"), (UNSUBSCRIBED, "Unsubscribed"),

    ]
    email = models.EmailField()
    status = models.IntegerField(choices=TYPE_CHOICES)


class EmailEntry(models.Model):
    """
    Represents an digest digest email entry.
    """
    DRAFT, PENDING, PUBLISHED = 0, 1, 2

    # The email entry may be posted as an entry.
    post = models.ForeignKey(Post, null=True)

    # This is a simplified text content of the Post body.
    text = models.TextField(default='')

    # The data the entry was created at.
    creation_date = models.DateTimeField(auto_now_add=True)

    # The date the email was sent
    sent_at = models.DateTimeField(null=True, blank=True)

    # The date the email was sent
    status = models.IntegerField(choices=((DRAFT, "Draft"), (PUBLISHED, "Published")))


class PostAdmin(admin.ModelAdmin):
    list_display = ('lastedit_date', 'html', 'title', 'type', 'author')
    fieldsets = (
        (None, {'fields': ('title',)}),
        ('Attributes', {'fields': ('type', 'status', 'real_reply_count', 'sticky', 'isfront', 'tag_val', 'tag_set',)}),
        ('Content', {'fields': ('content', )}),
    )
    search_fields = ('title', 'author__name')

admin.site.register(Post, PostAdmin)


class PostView(models.Model):
    """
    Keeps track of post views based on IP address.
    """
    ip = models.GenericIPAddressField(default='', null=True, blank=True)
    post = models.ForeignKey(Post, related_name="post_views")
    date = models.DateTimeField(auto_now=True)


class Vote(models.Model):
    # Post statuses.
    UP, DOWN, BOOKMARK, ACCEPT = range(4)
    TYPE_CHOICES = [(UP, "Upvote"), (DOWN, "Downvote"), (BOOKMARK, "Bookmark"), (ACCEPT, "Accept")]
    

    author = models.ForeignKey(settings.AUTH_USER_MODEL)
    post = models.ForeignKey(Post, related_name='votes')
    type = models.IntegerField(choices=TYPE_CHOICES, db_index=True)
    date = models.DateTimeField(db_index=True, auto_now=True)

    def __unicode__(self):
        return u"Vote: %s, %s, %s" % (self.post_id, self.author_id, self.get_type_display())

class VoteAdmin(admin.ModelAdmin):
    list_display = ('author', 'post', 'type', 'date')
    ordering = ['-date']
    search_fields = ('post__title', 'author__name')


admin.site.register(Vote, VoteAdmin)

class SubscriptionManager(models.Manager):
    def get_subs(self, post):
        "Returns all suscriptions for a post"
        return self.filter(post=post.root).select_related("user")

# This contains the notification types.
from biostar.const import LOCAL_MESSAGE, MESSAGING_TYPE_CHOICES


class Subscription(models.Model):
    "Connects a post to a user"

    class Meta:
        unique_together = (("user", "post"),)

    user = models.ForeignKey(settings.AUTH_USER_MODEL, verbose_name=_("User"), db_index=True)
    post = models.ForeignKey(Post, verbose_name=_("Post"), related_name="subs", db_index=True)
    #0 站内信  1邮箱
    type = models.IntegerField(choices=MESSAGING_TYPE_CHOICES, default=LOCAL_MESSAGE, db_index=True)
    date = models.DateTimeField(_("Date"), db_index=True)

    objects = SubscriptionManager()

    def __unicode__(self):
        return "%s to %s" % (self.user.name, self.post.title)

    def save(self, *args, **kwargs):

        if not self.id:
            # Set the date to current time if missing.
            self.date = self.date or const.now()

        super(Subscription, self).save(*args, **kwargs)


    @staticmethod
    def get_sub(post, user):

        if user.is_authenticated():
            try:
                return Subscription.objects.get(post=post, user=user)
            except ObjectDoesNotExist, exc:
                return None

        return None

    @staticmethod
    def create(sender, instance, created, *args, **kwargs):
        "Creates a subscription of a user to a post"
        user = instance.author
        root = instance.root
        if Subscription.objects.filter(post=root, user=user).count() == 0:
            sub_type = user.profile.message_prefs
            if sub_type == const.DEFAULT_MESSAGES:
                sub_type = const.EMAIL_MESSAGE if instance.is_toplevel else const.LOCAL_MESSAGE
            sub = Subscription(post=root, user=user, type=sub_type)
            sub.date = datetime.datetime.utcnow().replace(tzinfo=utc)
            sub.save()
            # Increase the subscription count of the root.
            Post.objects.filter(pk=root.id).update(subs_count=F('subs_count') + 1)

    @staticmethod
    def finalize_delete(sender, instance, *args, **kwargs):
        # Decrease the subscription count of the post.
        Post.objects.filter(pk=instance.post.root_id).update(subs_count=F('subs_count') - 1)



# Admin interface for subscriptions
class SubscriptionAdmin(admin.ModelAdmin):
    search_fields = ('user__name', 'user__email')
    list_select_related = ["user", "post"]


admin.site.register(Subscription, SubscriptionAdmin)

# Data signals
from django.db.models.signals import post_save, post_delete, m2m_changed

post_save.connect(Post.check_root, sender=Post)
#post_save.connect(Subscription.create, sender=Post, dispatch_uid="create_subs")
post_delete.connect(Subscription.finalize_delete, sender=Subscription, dispatch_uid="delete_subs")
#m2m_changed.connect(Tag.update_counts, sender=Post.tag_set.through)

