# -*- coding: utf-8 -*- 
# Create your views here.
from django.shortcuts import render_to_response
from django.views.generic import TemplateView, DetailView, ListView, FormView, UpdateView
from .models import Post, Report,Subscription
from biostar.apps.users.models import User, Watch, Block, Profile
from biostar import const
from django.db.models import signals, Q
from biostar.server.models import *
from biostar.server.moderate import *
from biostar.apps.util.util import *
from biostar.apps.util.views import *
from biostar.apps import signal
from django import forms
from django.core.urlresolvers import reverse
from crispy_forms.helper import FormHelper
from crispy_forms.layout import Layout, Field, Fieldset, Div, Submit, ButtonHolder
from django.shortcuts import render
from django.http import HttpResponseRedirect, HttpResponse, HttpRequest
from django.contrib import messages
from . import auth
from braces.views import LoginRequiredMixin
from datetime import datetime, timedelta
import pytz
from django.utils.timezone import utc
from django.core.exceptions import ObjectDoesNotExist
from django.conf import settings
from biostar.const import OrderedDict
from django.core.exceptions import ValidationError
from django.contrib.auth.decorators import login_required
from django.views.decorators.csrf import csrf_exempt
import os, re
import logging, json, traceback
from biostar.apps.util import now

import langdetect
from biostar import celery
from django.template.loader import render_to_string
import requests
from sets import Set

#english only.
def english_only(text):
    try:
        text.decode('ascii')
    except Exception:
        raise ValidationError('Title may only contain plain text (ASCII) characters')


def valid_language(text):
    supported_languages = settings.LANGUAGE_DETECTION
    if supported_languages:
        lang = langdetect.detect(text)
        if lang not in supported_languages:
            raise ValidationError(
                    'Language "{0}" is not one of the supported languages {1}!'.format(lang, supported_languages))

logger = logging.getLogger(__name__)


def valid_title(text):
    "Validates form input for tags"
    text = text.strip()
    if not text:
        raise ValidationError('Please enter a title')

    if len(text) < 5:
        raise ValidationError('The title is too short')

    #words = text.split(" ")
    #if len(words) < 3:
    #    raise ValidationError('More than two words please.')


def valid_tag(text):
    "Validates form input for tags"
    text = text.strip()
    if not text:
        raise ValidationError('请输入至少一个相关的话题类别')
    if len(text) > 50:
        raise ValidationError('话题类别栏总字数不能超过50个字')
    words = text.split(",")
    if len(words) > 5:
        raise ValidationError('话题类别数量不能超过5个')

class PagedownWidget(forms.Textarea):
    TEMPLATE = "pagedown_widget.html"

    def render(self, name, value, attrs=None):
        value = value or ''
        rows = attrs.get('rows', 15)
        klass = attrs.get('class', '')
        params = dict(value=value, rows=rows, klass=klass)
        return render_to_string(self.TEMPLATE, params)


class LongForm(forms.Form):
    FIELDS = "title content post_type tag_val".split()

    POST_CHOICES = [(Post.QUESTION, "问题")
                    #(Post.JOB, "Job Ad"),
                    #(Post.TUTORIAL, "Tutorial"), (Post.TOOL, "Tool"),
                    #(Post.FORUM, "Forum"), (Post.NEWS, "News"),
                    #(Post.BLOG, "Blog"), (Post.PAGE, "Page")
                    ]

    title = forms.CharField(
        label="标题",
        max_length=50, min_length=5, validators=[valid_title],
        help_text="- 问题描述（请尽量用平和，中性的描述语言），不多于50字 ")

    post_type = forms.ChoiceField(
        label="类型",
        choices=POST_CHOICES, help_text="")

    tag_val = forms.CharField(
        label="话题分类",
        required=True, validators=[valid_tag],
        help_text="- 搜索并显示下拉框中可选话题类别列表，选择该问题所属的话题类别，数量不超过3个",
    )

    content = forms.CharField(widget=PagedownWidget, validators=[
                                                        #valid_language
                                                         ],
                              min_length=10, max_length=15000,
                              label="问题描述", help_text="- 请尽量详细的描述你的问题，必要时候可以添加引用，图片或视频等加以说明。 描述不少于10字",)

    def __init__(self, *args, **kwargs):
        super(LongForm, self).__init__(*args, **kwargs)
        self.helper = FormHelper()
        self.helper.form_class = "post-form"
        self.helper.layout = Layout(
            Fieldset(
                '发起提问',
                Field('title'),
                Field('post_type'),
                Field('tag_val'),
                Field('content'),
            ),
            ButtonHolder(
                Submit('submit', '提交问题')
            )
        )


class ShortForm(forms.Form):
    FIELDS = ["content", "mainpost_id"]

    content = forms.CharField(widget=PagedownWidget, min_length=3, max_length=100000,)

    mainpost_id = forms.IntegerField(
        min_value=1, validators=[],
        help_text="valid id.")

    def __init__(self, *args, **kwargs):
        super(ShortForm, self).__init__(*args, **kwargs)
        self.helper = FormHelper()
        self.helper.layout = Layout(
            Fieldset(
                'Post',
                'content',
                'mainpost_id',
            ),
            ButtonHolder(
                Submit('submit', 'Submit')
            )
        )


def parse_tags(category, tag_val):
    pass


@login_required
@csrf_exempt
def external_post_handler(request):
    "This is used to pre-populate a new form submission"
    import hmac

    user = request.user
    home = reverse("home")
    name = request.REQUEST.get("name")

    if not name:
        messages.error(request, "Incorrect request. The name parameter is missing")
        return HttpResponseRedirect(home)

    try:
        secret = dict(settings.EXTERNAL_AUTH).get(name)
    except Exception, exc:
        logger.error(exc)
        messages.error(request, "Incorrect EXTERNAL_AUTH settings, internal exception")
        return HttpResponseRedirect(home)

    if not secret:
        messages.error(request, "Incorrect EXTERNAL_AUTH, no KEY found for this name")
        return HttpResponseRedirect(home)

    content = request.REQUEST.get("content")
    submit = request.REQUEST.get("action")
    digest1 = request.REQUEST.get("digest")
    digest2 = hmac.new(secret, content).hexdigest()

    if digest1 != digest2:
        messages.error(request, "digests does not match")
        return HttpResponseRedirect(home)

    # auto submit the post
    if submit:
        post = Post(author=user, type=Post.QUESTION)
        for field in settings.EXTERNAL_SESSION_FIELDS:
            setattr(post, field, request.REQUEST.get(field, ''))
        post.save()
        post.add_tags(post.tag_val)
        return HttpResponseRedirect(reverse("post-details", kwargs=dict(pk=post.id)))

    # pre populate the form
    sess = request.session
    sess[settings.EXTERNAL_SESSION_KEY] = dict()
    for field in settings.EXTERNAL_SESSION_FIELDS:
        sess[settings.EXTERNAL_SESSION_KEY][field] = request.REQUEST.get(field, '')

    return HttpResponseRedirect(reverse("new-post"))


def stub_update_reply_count(post):
    new_reply_count = Post.objects.filter(root_id=post.root_id, type=Post.ANSWER, status=Post.OPEN).count()
    post.root.real_reply_count = new_reply_count
    post.root.save()

def stub_update_user_answer_count(user):
    new_ans_count = Post.objects.filter(author=user, type=Post.ANSWER, status=Post.OPEN).count()
    user.cnt_answer = new_ans_count
    user.save()

class NewPost(LoginRequiredMixin, FormView):
    form_class = LongForm
    template_name = "post_edit.html"

    def get(self, request, *args, **kwargs):
        logger.info("%s user %s new post get %s" % (get_ip(request), request.user, kwargs))

        initial = dict()

        if not self.request.user.is_trusted: 
            self.template_name = "single_message.html"
            msg = "对不起，您的账号目前尚处于初级阶段，为了保证社区内容的质量，您目前只可以回答和评论，\
            且每6个小时发帖数量不超过5个。当您的积分到达一定数值时，就可以获得提问权限。您认真\
            和有质量的内容会获得他人的赞并增加您的积分，但低质量重复等的内容也会损害您的积分。"
            icon = "fa fa-smile-o"
            return render(request, self.template_name, {'msg': msg, 'icon': icon})

        # Attempt to prefill from GET parameters
        for key in "title tag_val content".split():
            value = request.GET.get(key)
            if value:
                initial[key] = value


        # Attempt to prefill from external session
        sess = request.session
        if settings.EXTERNAL_SESSION_KEY in sess:
            for field in settings.EXTERNAL_SESSION_FIELDS:
                initial[field] = sess[settings.EXTERNAL_SESSION_KEY].get(field)
            del sess[settings.EXTERNAL_SESSION_KEY]

        form = self.form_class(initial=initial)
        return render(request, self.template_name, {'form': form, 'form_err':''})


    def post(self, request, *args, **kwargs):
        logger.info("%s user %s new post post %s" % (get_ip(request), request.user, kwargs))

        # Validating the form.
        form = self.form_class(request.POST)

        if not form.is_valid():
            import bleach
            errstr = bleach.clean(str(form.errors).replace('tag_val', '标签').replace('This field is required.', '不能为空')\
                ).replace('<ul>', '').replace('</ul>', '').replace('<li>', '').replace('</li>', '')\
            .replace('The title is too short', '标题请在5-15字内').replace('title','')
            return render(request, self.template_name, {'form_err': errstr})


        # Valid forms start here.
        data = form.cleaned_data.get

        title = data('title')
        content = data('content')        
        post_type = int(data('post_type'))
        tag_val = data('tag_val').replace(',', ' ')

        not_pool = 0
        try:
            if request.POST['not_pool'] == '1':
                not_pool = 1
        except Exception, exc:
            not_pool = 0
            
        status = Post.TOOPEN
        if not_pool:
            status = Post.OPEN

        post = Post(
            title=title, content=content, tag_val=tag_val,
            author=request.user, type=post_type, status=status
        )
        post.save()
        #owner subscribe its question
        #Subscription.objects.create(post=post, user=request.user, type=const.LOCAL_MESSAGE)

        request.user.cnt_question += 1
        request.user.save()

        # Triggers a new post save.
        post.add_tags(post.tag_val)

        
        celery.notify_imgserver.delay(post.id)

        if post.status == Post.OPEN:
            celery.send_mentionuser_msg.delay(request.user, post, content, post.type)

        #async push to users who follow the topics
        #！！！！！for now, cannot find propeer results, need more research
        #aa = Profile.objects.filter(tags__name__in=['政治']) #tags is m2m relationship
        #paginew = Paginator(aa, self.paginate_by).page(1)
        #posts = paginew.object_list
        #bb = aa.object_list
        tags = tag_val.split() #for now only push first tag
        celery.new_question_feed_push.delay(request.user, post, tags)



        self.template_name = "post_edit_postcreate.html"
        return render(request, self.template_name, {})


class NewAnswer(LoginRequiredMixin, FormView):
    """
    Creates a new post.
    """
    form_class = ShortForm
    template_name = "post_edit.html"
    type_map = dict(question=Post.QUESTION, answer=Post.ANSWER, comment=Post.COMMENT)
    post_type = None

    def get(self, request, *args, **kwargs):
        logger.info("%s user %s new ans get %s" % (get_ip(request), request.user, kwargs))
        initial = {}

        # The parent id.
        pid = int(self.kwargs['pid'])
        # form_class = ShortForm if pid else LongForm
        form = self.form_class(initial=initial)
        return render(request, self.template_name, {'form': form})

    def post(self, request, *args, **kwargs):
        logger.info("%s user %s new ans post %s" % (get_ip(request), request.user, kwargs))
        pid = int(self.kwargs['pid'])
        create_draft = int(self.request.GET.get('d', 0))

        # Find the parent.
        try:
            parent = Post.objects.get(pk=pid)
            root = parent.root
        except ObjectDoesNotExist, exc:
            logger.error("%s user %s newans not exist %s" % (get_ip(request), request.user, kwargs))
            return HttpResponseRedirect("/")
        #import pdb
        #pdb.set_trace()
        data = request.POST
        content = data['content']
        mainpost = None

        # Figure out the right type for this new post, passed in url as_view param
        if parent.status != Post.OPEN and parent.status != Post.TOOPEN:
            ret={'r':0, 'm': '对不起，该问题当前没有开放'}
            return HttpResponse(json.dumps(ret), content_type = "application/json")

        post_type = self.type_map.get(self.post_type)
        if post_type == Post.COMMENT:
            is_block = Block.objects.filter(blocking_user = parent.author, blocked_user = self.request.user)
            if is_block:
                ret={'r':0, 'm': '对不起，对方已经屏蔽了您'}
                return HttpResponse(json.dumps(ret), content_type = "application/json")

            # Validating the form.
            form = self.form_class(request.POST)
            if not form.is_valid():
                #return render(request, self.template_name, {'form': form})
                #tasks = Task.objects.all()
                #data = serializers.serialize("json", tasks)
                ret={'r':0, 'm': '格式不正确，如字数应该在3-1000字之内'}
                return HttpResponse(json.dumps(ret), content_type = "application/json")

            # Valid forms start here.
            data = form.cleaned_data.get

            mainpost_id = data('mainpost_id')
            mainpost = Post.objects.get(pk=mainpost_id)
            content=data('content')

        elif post_type == Post.ANSWER:
            #judge if answered or not. 
            answers = Post.objects.filter(type=Post.ANSWER, root_id=pid, author=request.user, status=Post.OPEN)
            if len(answers) >0:
                ret={'r':0, 'm': '您已经回答过了'}
                return HttpResponse(json.dumps(ret), content_type = "application/json")

            data = request.POST
            content=data['content']
            if len(content.replace('&nbsp; ', '')) < 50:
                ret={'r':0, 'm': '字数不能少于50字'}
                return HttpResponse(json.dumps(ret), content_type = "application/json")
            mainpost = None

        #if draft found, just update its content and status
        drafts = Post.objects.filter(type=Post.ANSWER, root_id=pid, author=request.user, status=Post.DRAFT)
        if len(drafts) >0 and post_type == Post.ANSWER:
            draft = drafts[0]
            if draft.status==Post.OPEN or draft.status==Post.DRAFT:
                draft.status = Post.OPEN
            else: #if pending or hidden, save pending for audit
                draft.status = Post.PENDING
            draft.content = content
            if draft.status == Post.OPEN:
                draft.save(update=True)
            else:
                draft.save()
            #edit do not notify for now
            #signal.signal_draft_to_open.send(sender=None, instance=draft, created=True)
            post = draft
        else:
            # Create a new post.
            if create_draft:#if frontend says it's draft, save draft style 
                post = Post(
                    title=parent.title, content=content, author=request.user, type=post_type,
                    parent=parent, parent_authorid=parent.author_id, parent_authorname=parent.author.name, 
                    mainpost=mainpost, 
                    mainpost_authorid=mainpost.author_id if mainpost else 0, 
                    mainpost_authorname=mainpost.author.name if mainpost else 0, 
                    root_authorid=root.author_id, root_authorname=root.author.name, status=Post.DRAFT,
                )
                post.save()
            else:
                post = Post(
                    title=parent.title, content=content, author=request.user, type=post_type,
                    parent=parent, parent_authorid=parent.author_id, parent_authorname=parent.author.name, 
                    mainpost=mainpost, 
                    mainpost_authorid=mainpost.author_id if mainpost else 0, 
                    mainpost_authorname=mainpost.author.name if mainpost else 0, 
                    root_authorid=root.author_id, root_authorname=root.author.name, 
                )
                post.save(update=True)


        if post.status == Post.OPEN:
            if post.type == Post.ANSWER:
                stub_update_user_answer_count(request.user)
                if post.parent.type == Post.NEWS:
                    post.title = '%s“%s”?'%("如何看待", post.parent.title)
                    post.save()
            elif post.type == Post.COMMENT:
                request.user.cnt_cmt += 1
            request.user.save()


        post.is_editable = True #editable for user who created it
        post.has_bookmark = False
        post.has_upvote = False
        post.can_accept = False


        if post.type == Post.ANSWER and post.status == Post.OPEN:
            cache_post_detail(post.root, None, 5, 60*10)
            stub_update_reply_count(post)


            # Reset the timestamps on the parent
            # if 60 days old, no update parent's edittime
            #if self.type == Post.ANSWER and self.status == Post.OPEN:
            notouchtime = post.parent.creation_date + timedelta(days=3)
            if post.parent.status == Post.TOOPEN:
            	notouchtime = post.parent.creation_date + timedelta(days=30)
            if (notouchtime > now()):
                logger.info("update parent edittime 1: %u %s"%(post.parent.id, post.author.name))
                post.root.lastedit_date = post.lastedit_date
                post.root.lastedit_user = post.lastedit_user
                post.root.save()

            #async push to auther's followers if not pushed
            celery.new_answer_feed_push.delay(request.user, post)


        if post.type == Post.QUESTION and post.status == Post.OPEN:
            Tag.update_counts("post_add", post)


        if (post.type == Post.QUESTION or post.type == Post.ANSWER) and post.status == Post.OPEN:
            celery.notify_imgserver.delay(post.id)
        if post.status == Post.OPEN:
            celery.send_mentionuser_msg.delay(request.user, post, content, post.type)

        #add answer submitter to subscription of this question.
        if post.type == Post.ANSWER and post.status == Post.OPEN:
            subs = Subscription.objects.filter(post=post.root, user=request.user)
            if subs:
                subs.update(type=const.LOCAL_MESSAGE)
            else:
                Subscription.objects.create(post=post.root, user=request.user, type=const.LOCAL_MESSAGE)
                post.root.subs_count += 1
                post.root.save()
                request.user.cnt_watchpost += 1
                request.user.save()

        #pid: parent id, id: post id
        ret={'r':1, 'm': '', 'id': post.id, 
             'p_id': pid, 'p_n': parent.author.name, 'p_uid': parent.author.id,
             'p_uname':parent.author.name, 'p_t': post.type,
             'p_thumb': settings.UPLOAD_IMG_STATIC_FOLDER + "small50-"+post.author.profile.thumbnail,
             'p_h': render_to_string("post_single_answer.html", dict(post=post, user=request.user))}
        return HttpResponse(json.dumps(ret), content_type = "application/json")


class EditPost(LoginRequiredMixin, FormView):
    """
    Edits an existing post.
    """

    # The template_name attribute must be specified in the calling apps.
    template_name = "post_edit.html"
    form_class = LongForm

    def get(self, request, *args, **kwargs):
        logger.info("%s user %s edit post get %s" % (get_ip(request), request.user, kwargs))
        initial = {}

        pk = int(self.kwargs['pk'])
        post = Post.objects.get(pk=pk)
        post = auth.post_permissions(request=request, post=post)

        # Check and exit if not a valid edit.
        if not post.is_editable:
            logger.error("%s user %s editpost no auth %s" % (get_ip(request), request.user, kwargs))
            return HttpResponseRedirect(reverse("home"))

        initial = dict(title=post.title, content=post.content, post_type=post.type, tag_val=post.tag_val)

        # Disable rich editing for preformatted posts
        pre = 'class="preformatted"' in post.content
        #form_class = LongForm if post.is_toplevel else ShortForm
        #form = form_class(initial=initial)
        return render(request, self.template_name, {'post': post, 'pre': pre})

    def post(self, request, *args, **kwargs):
        logger.info("%s user %s edit ans post %s" % (get_ip(request), request.user, kwargs))
        #import pdb
        #pdb.set_trace()
        #todo!! comment cannot be update
        pk = int(self.kwargs['pk'])
        edit_draft = int(self.request.GET.get('d', 0))

        try:
            post = Post.objects.get(pk=pk)
        except ObjectDoesNotExist, exc:
            logger.error("%s user %s editpost not exist %s" % (get_ip(request), request.user, kwargs))
            ret={'r':0, 'm': '该帖子不存在，可能已经被删除'}
            return HttpResponse(json.dumps(ret), content_type = "application/json")

        pre_status = post.status
        post = auth.post_permissions(request=request, post=post)

        user = request.user

        # For historical reasons we had posts with iframes
        # these cannot be edited because the content would be lost in the front end
        # if "<iframe" in post.content:
        #     messages.error(request, "This post is not editable because of an iframe! Contact if you must edit it")
        #     ret={'r':0, 'm': '非法操作：包含iframe'}
        #     return HttpResponse(json.dumps(ret), content_type = "application/json")

        # Check and exit if not a valid edit.
        if not post.is_editable and not user.is_moderator:
            logger.error("%s user %s editpost no auth %s" % (get_ip(request), request.user, kwargs))
            ret={'r':0, 'm': '你无权操作'}
            return HttpResponse(json.dumps(ret), content_type = "application/json")
        if post.status == Post.CLOSED:
            logger.error("%s user %s editpost no auth %s" % (get_ip(request), request.user, kwargs))
            ret={'r':0, 'm': '已关闭的问题不可以修改内容'}
            return HttpResponse(json.dumps(ret), content_type = "application/json")
        #normal user cannot edit question  
        #elif not (user.is_moderator or user.is_staff) and post.type == Post.QUESTION:
        #    logger.error("%s user %s editpost no auth admin %s" % (get_ip(request), request.user, kwargs))
        #    ret={'r':0, 'm': '只有管理员才可以修改问题'}
        #    return HttpResponse(json.dumps(ret), content_type = "application/json")
        elif post.type == Post.COMMENT:
            logger.error("%s user %s editpost cmt not allowed edit %s" % (get_ip(request), request.user, kwargs))
            ret={'r':0, 'm': '评论不可以修改'}
            return HttpResponse(json.dumps(ret), content_type = "application/json")

        '''
        # Posts with a parent are not toplevel
        form_class = LongForm if post.is_toplevel else ShortForm

        form = form_class(request.POST)
        if not form.is_valid():
            # Invalid form submission.
            return render(request, self.template_name, {'form': form})

        # Valid forms start here.
        data = form.cleaned_data

        # Set the form attributes.
        for field in form_class.FIELDS:
            setattr(post, field, data[field])
        '''
        data = request.POST
        content = data['content']
        title = data['title']
        mainpost = None

        if title != "0":
            if len(title) < 5 or len(title) > 100:
                ret={'r':0, 'm': '标题长度应该为5-50字之间'}
                return HttpResponse(json.dumps(ret), content_type = "application/json")

        if post.type == Post.ANSWER:
            if len(content.replace(u'\xa0', '')) < 50:
                ret={'r':0, 'm': '字数不能少于50字'}
                return HttpResponse(json.dumps(ret), content_type = "application/json")
        elif post.type == Post.QUESTION:
            if len(content) < 10:
                ret={'r':0, 'm': '字数不能少于10字'}
                return HttpResponse(json.dumps(ret), content_type = "application/json")

        if not (user.is_moderator or user.is_staff) and post.type == Post.QUESTION:
            nochangetime = post.creation_date + timedelta(hours=120)
            if(nochangetime < now()):
                ret={'r':0, 'm': '5天以上的问题不可以被修改'}
                return HttpResponse(json.dumps(ret), content_type = "application/json")

        if edit_draft: #if edit draft, keep status as DRAFT.  
            if post.status==Post.OPEN:
                post.status = Post.DRAFT
            #else: #if pending draft hidden, keep as is
            # TODO: fix this oversight!
            if post.type == Post.QUESTION:
                if post.status == Post.TOOPEN:
                    post.status = Post.TOOPEN
                else:
                    post.status = Post.OPEN #question always open

            post.content = content
            #only admin can change title of question
            if title != "0" and request.user.is_moderator and post.type == Post.QUESTION \
            or title != "0" and  post.type == Post.BLOG \
            or title != "0" and  post.type == Post.NEWS: 
                post.title = title
            post.status = post.status #otherwise it will set to PENDING

            # This is needed to validate some fields.
            post.save(update=False)
        else:
            #if post status is OPEN or DRAFT, set as OPEN. leave other status as is.
            if post.status == Post.OPEN or post.status == Post.DRAFT:
                post.status = Post.OPEN 
            elif post.status == Post.HIDDEN:
                post.status = Post.PENDING

            if post.type == Post.QUESTION:
                if post.status == Post.TOOPEN:
                    post.status = Post.TOOPEN
                else:
                    post.status = Post.OPEN #question always open
            # TODO: fix this oversight!
            post.content = content
            if title != "0" and request.user.is_moderator and post.type == Post.QUESTION \
            or title != "0" and  post.type == Post.BLOG \
            or title != "0" and  post.type == Post.NEWS:
                post.title = title
            post.status = post.status #otherwise it will set to PENDING

            # This is needed to validate some fields.
            post.save(update=False) #edit: means already update stats before
       

        if post.is_toplevel:
            post.add_tags(post.tag_val)

        # Update the last edit user.
        post.lastedit_user = request.user
        post.status = post.status

        if post.type == Post.ANSWER and post.status != pre_status:
            cache_post_detail(post.root, None, 5, 60*10)
            #update reply_count

	    stub_update_reply_count(post)
        
        # Only editing by admin/moderator can bumps the post(question)
        if post.type == Post.QUESTION and request.user.is_moderator:
            logger.info("update parent edittime 2: %u %s"%(post.id, request.user))
            post.lastedit_date = datetime.utcnow().replace(tzinfo=utc)

            if post.status == Post.OPEN and pre_status != Post.OPEN:
                Tag.update_counts("post_add", post)

            #update all answers title as well
            ques_ans = Post.objects.filter(root_id = post.id, type=Post.ANSWER).all()
            for a in ques_ans:
                a.title = "A: %s"%post.title
                a.save()
        elif post.type == Post.ANSWER:
            post.lastedit_date = datetime.utcnow().replace(tzinfo=utc)

        post.save(update=False)
 

        #fetch image 
        if (post.type == Post.QUESTION or post.type == Post.ANSWER or post.type == Post.BLOG or post.type == Post.NEWS) and (post.status == Post.OPEN or post.status == Post.TOOPEN):
            celery.notify_imgserver.delay(post.id)


        #async push to auther's followers if not pushed
        #if new state is open, update its content/status
        #if state is draft, update its content to "user has hidden it"
        if post.type == Post.ANSWER and post.status == Post.OPEN:
            celery.update_answer_feed_push.delay(post, 0)
        elif post.type == Post.ANSWER and pre_status == Post.OPEN and post.status == Post.DRAFT:
            celery.update_answer_feed_push.delay(post,  1)

        ret={'r':1, 'm': '', 'p_h':post.html}
        return HttpResponse(json.dumps(ret), content_type = "application/json")

    def get_success_url(self):
        return reverse("user_details", kwargs=dict(pk=self.kwargs['pk']))


class DeletePost(LoginRequiredMixin, FormView):
    """
    Delete a new post.
    Note: delete is not real remove. is set STAUTS as DELETED(3)
    """
    type_map = dict(question=Post.QUESTION, answer=Post.ANSWER, comment=Post.COMMENT)
    post_type = None

    def post(self, request, *args, **kwargs):
        logger.info("%s user %s delete post post %s" % (get_ip(request), request.user, kwargs))
        initial = {}
        #import pdb
        #pdb.set_trace()

        # The parent id.
        pk = int(self.kwargs['pk'])
        post = Post.objects.get(pk=pk)
        user = request.user
        ret={'r':0, 'm': '发生错误啦'}

        post = auth.post_permissions(request=request, post=post)       
        if not post.is_editable:
            logger.error("%s user %s delpost no auth %s" % (get_ip(request), request.user, kwargs))
            ret={'r':0, 'm': '你无权操作'}
            return HttpResponse(json.dumps(ret), content_type = "application/json")
        #normal user cannot delete question  
        elif not (user.is_moderator or user.is_staff) and post.type == Post.QUESTION:
            logger.error("%s user %s delpost q cannot delete %s" % (get_ip(request), request.user, kwargs))
            ret={'r':0, 'm': '问题不可以被删除'}
            return HttpResponse(json.dumps(ret), content_type = "application/json")

        ptype = post.type
        if post.status == Post.DELETED: #already deleted
            logger.error("%s user %s delpost already deleted %s" % (get_ip(request), request.user, kwargs))
            return HttpResponse(json.dumps(ret), content_type = "application/json")

        pretype = post.type
        rootp = post.root
        #try: 
        if (post.author.id == user.id and post.vote_count <=0 and post.comment_count <=0) or ptype == Post.COMMENT or post.status == Post.DRAFT:#real delete comment
            post.delete()
        else:
            post.fake_delete()
        # except:
        #     logger.error("%s user %s delpost error %s" % (get_ip(request), request.user, kwargs))
        #     ret={'r':0, 'm': '发生错误啦'}
        #     return HttpResponse(json.dumps(ret), content_type = "application/json")

        if pretype == Post.ANSWER:
            cache_post_detail(rootp, None, 5, 60*10)
            stub_update_reply_count(post)
        #update user's stats counts
        if ptype == Post.QUESTION:
            celery.del_question_feed_push.delay(pk)
            request.user.cnt_question = request.user.cnt_question if request.user.cnt_question == 0 else request.user.cnt_question-1
        elif ptype == Post.ANSWER:
            celery.del_answer_feed_push.delay(pk)
            stub_update_user_answer_count(request.user)
        elif ptype == Post.COMMENT:
            request.user.cnt_cmt = request.user.cnt_cmt if request.user.cnt_cmt == 0 else request.user.cnt_cmt-1
        elif ptype == Post.IDEA:
            celery.del_idea_feed_push.delay(pk)
        request.user.save()

        if ptype == Post.QUESTION:
            Tag.update_counts("post_remove", post)


        #todo, if answer, delete all cmts

        ret={'r':1, 'm': ''}
        return HttpResponse(json.dumps(ret), content_type = "application/json")


class ReportPost(LoginRequiredMixin, FormView):

    def post(self, request, *args, **kwargs):
        logger.info("%s user %s report post post %s" % (get_ip(request), request.user, kwargs))
        #import pdb
        #pdb.set_trace()
        #todo!! comment cannot be update
        pk = int(self.kwargs['pk'])
        type = int(self.request.GET.get('t', 0))
        if type > Report.VIEWONLY:
            logger.error("%s user %s reportpost type error %s" % (get_ip(request), request.user, kwargs))
            ret={'r':0, 'm': '举报类型错误'}
            return HttpResponse(json.dumps(ret), content_type = "application/json")

        user = request.user

        #GEORGE: todo, add moderate limit, ie, cannot report >10 posts per day
        try:
            post = Post.objects.get(pk=pk)
        except ObjectDoesNotExist, exc:
            logger.error("%s user %s reportpost not exist %s" % (get_ip(request), request.user, kwargs))
            ret={'r':0, 'm': '该帖子不存在，可能已经被删除'}
            return HttpResponse(json.dumps(ret), content_type = "application/json")

        report = Report(
            user_id=user.id, type=type, post_id = pk, date=datetime.utcnow().replace(tzinfo=utc),
        )
        report.save()
        update_related_scores(post, user, post.author, 2, subtype=1)

        ret={'r':1, 'm': '举报成功'}
        return HttpResponse(json.dumps(ret), content_type = "application/json")


class NewIdea(LoginRequiredMixin, FormView):
    """
    Creates a new post.
    """

    def post(self, request, *args, **kwargs):
        logger.info("%s user %s new idea post %s" % (get_ip(request), request.user, kwargs))

        #import pdb
        #pdb.set_trace()
        data = request.POST
        content = data['content']
        video = data['video']
        is_global = int(data['global'])
        mainpost = None

        # Figure out the right type for this new post, passed in url as_view param
        if len(content) > 500 :
            ret={'r':0, 'm': '字数不可以超过200个汉字'}
            return HttpResponse(json.dumps(ret), content_type = "application/json")
        m = re.compile('(https?:\/\/(?:www\.|(?!www))[a-zA-Z0-9][a-zA-Z0-9-]+[a-zA-Z0-9]\.[^\s]{2,}|www\.[a-zA-Z0-9][a-zA-Z0-9-]+[a-zA-Z0-9]\.[^\s]{2,}|https?:\/\/(?:www\.|(?!www))[a-zA-Z0-9]\.[^\s]{2,}|www\.[a-zA-Z0-9]\.[^\s]{2,})')
        matches = m.findall(content)
        for match in matches: 
            content = content.replace(match, "<a target='new' href='%s'>%s</a>"%(match, "<i class='fa fa-link'></i> 链接"))

        pic_url = ""
        try:
            pic_code = data['pic']
            if not pic_code.startswith('data:image'):
                ret={'r':0, 'm': '上传图片格式错误，只支持JPG、PNG、GIF格式。'}
                return HttpResponse(json.dumps(ret), content_type = "application/json")

            filename = "idea-pic-%s%s.png"%(request.user.id, hashlib.sha1(datetime.now().isoformat()).hexdigest())
            save_file(parse_input(data['pic']), filename, 0, settings.UPLOAD_IMG_FOLDER)
            newfile = "small-%s"%filename
            processor = CompressImage()            
                        
            pic_url = ""
            #TODO create folder by month
            if not processor.processfile(settings.UPLOAD_IMG_FOLDER, filename, newfile, "idea-image"):
                #if fail, use origin image.
                os.system("scp %s/%s%s peter@img:/home/peter/static/ideaimgs"%(settings.HOME_DIR, settings.UPLOAD_IMG_FOLDER, filename))
                pic_url = "%s"%(filename)
            else:    
                pic_url = "%s"%(newfile)
                os.system("scp %s/%s%s peter@img:/home/peter/static/ideaimgs"%(settings.HOME_DIR, settings.UPLOAD_IMG_FOLDER, pic_url))
            os.system("rm %s/%s%s"%(settings.HOME_DIR,settings.UPLOAD_IMG_FOLDER, filename))
            os.system("rm %s/%s%s"%(settings.HOME_DIR, settings.UPLOAD_IMG_FOLDER, newfile))
        except Exception, exc:
            pic_url = ""

        #extract video
        try:
            html_v = ""
            m = re.compile(".*(youtu.be\/|v\/|u\/\w\/|embed\/|watch\?v=|\&v=)([^#\&\?]*).*")
            matches = m.findall(video)
            if matches and len(matches[0][1]) == 11:
                html_v = '<p><iframe width="560" height="315" src="//www.youtube.com/embed/%s" frameborder="0" allowfullscreen></iframe></p>'%matches[0][1]
        except Exception, exc:
            html_v = ""

        post = Post(title="新鲜事动态", content="%s %s"%(content,html_v), author=request.user, type=Post.IDEA, status=Post.OPEN, attachimg=pic_url)
        if is_global and request.user.score >= 30:
            post.sticky = 1
            dt_now = datetime.now()
            midnight = datetime(dt_now.year, dt_now.month, dt_now.day) 
            myideas = Post.objects.filter(creation_date__gte=midnight, type=Post.IDEA, author=request.user).count()
            if myideas == 0:
                #24hour no idea, just minus 0
                request.user.score = request.user.score - 0
            else:
                request.user.score = request.user.score - 5
            request.user.save()
        post.root_id = post.id
        post.save()


        celery.new_idea_feed_push.delay(post, request.user)

        if post.status == Post.OPEN:
            celery.send_mentionuser_msg.delay(request.user, post, content, post.type)

         #pid: parent id, id: post id
        ret={'r':1, 'm': '', 'id': post.id, 
             'p_t': post.type,
             'p_thumb': settings.UPLOAD_IMG_STATIC_FOLDER + "small50-"+post.author.profile.thumbnail,
             'p_h': render_to_string("server_tags/post_idea_feed.html", dict(post=post, user=request.user))}
        return HttpResponse(json.dumps(ret), content_type = "application/json")



class NewColumn(LoginRequiredMixin, FormView):
    form_class = LongForm
    template_name = "post_comlun_edit.html"

    def get(self, request, *args, **kwargs):
        logger.info("%s user %s new column post get %s" % (get_ip(request), request.user, kwargs))

        initial = dict()

        if not self.request.user.is_trusted: 
            self.template_name = "single_message.html"
            msg = "对不起，您的账号目前尚处于初级阶段，为了保证社区内容的质量，您目前只可以回答和评论，\
            且每6个小时发帖数量不超过5个。当您的积分到达一定数值时，就可以获得提问权限。您认真\
            和有质量的回答会获得他人的赞并增加您的积分。"
            icon = "fa fa-smile-o"
            return render(request, self.template_name, {'msg': msg, 'icon': icon})

        # Attempt to prefill from GET parameters
        for key in "title tag_val content".split():
            value = request.GET.get(key)
            if value:
                initial[key] = value


        # Attempt to prefill from external session
        sess = request.session
        if settings.EXTERNAL_SESSION_KEY in sess:
            for field in settings.EXTERNAL_SESSION_FIELDS:
                initial[field] = sess[settings.EXTERNAL_SESSION_KEY].get(field)
            del sess[settings.EXTERNAL_SESSION_KEY]

        form = self.form_class(initial=initial)
        return render(request, self.template_name, {'form': form, 'form_err':''})


    def post(self, request, *args, **kwargs):
        logger.info("%s user %s new post column %s" % (get_ip(request), request.user, kwargs))



        title = self.request.POST.get('title', 0)
        content = self.request.POST.get('content', 0)       
        post_type = Post.BLOG
        tag_val = self.request.POST.get('tag_val', 0)
        status = Post.TOOPEN
        create_draft = int(self.request.GET.get('d', 0))
        mainpost = None



        if len(content) < 50 or len(content) > 200000:
            print( len(content))
            ret={'r':0, 'm': '字数不能少于50字或大于200000字'}
            return HttpResponse(json.dumps(ret), content_type = "application/json")

        
        tag_val = tag_val.replace(',', ' ')
        # Create a new post.
        if create_draft:#if frontend says it's draft, save draft style 
            post = Post(
                title=title, content=content, author=request.user, type=post_type,
                tag_val=tag_val, status=Post.DRAFT,
            )
            post.save()
            
        else:
            post = Post(
                title=title, content=content, author=request.user, type=post_type,
                tag_val=tag_val, 
            )
            post.save(update=True)

        post.mainpost_id = post.id
        post.root_id = post.id
        post.save()
        

        if post.status == Post.OPEN:
            if post.type == Post.ANSWER:
                stub_update_user_answer_count(request.user)
            elif post.type == Post.COMMENT:
                request.user.cnt_cmt += 1
            request.user.save()

        post.add_tags(post.tag_val)


        post.is_editable = True #editable for user who created it
        post.has_bookmark = False
        post.has_upvote = False
        post.can_accept = False
 

        if (post.type == Post.BLOG) and post.status == Post.OPEN:
            celery.notify_imgserver.delay(post.id)
        if post.status == Post.OPEN:
            celery.send_mentionuser_msg.delay(request.user, post, content, post.type)


        #pid: parent id, id: post id
        ret={'r':1, 'm': '', 'id': post.id,     
             'p_thumb': settings.UPLOAD_IMG_STATIC_FOLDER + "small50-"+post.author.profile.thumbnail,
             'p_h': render_to_string("post_single_answer.html", dict(post=post, user=request.user))}
        return HttpResponse(json.dumps(ret), content_type = "application/json")


class NewNews(LoginRequiredMixin, FormView):
    form_class = LongForm
    template_name = "post_news_edit.html"

    def get(self, request, *args, **kwargs):
        logger.info("%s user %s new news post get %s" % (get_ip(request), request.user, kwargs))
        os.system("echo 'addnews %s %s' >> accesslastanswer"%( request.user.name, datetime.utcnow()))
        initial = dict()
    
        if not self.request.user.is_moderator and not self.request.user.cnt_getvote > 800: 
            self.template_name = "single_message.html"
            msg = "对不起，只有管理员/获赞1000以上用户才能发布新闻"
            icon = "fa fa-smile-o"
            return render(request, self.template_name, {'msg': msg, 'icon': icon})

        #normal user can only post 1 per hour
        if not self.request.user.is_moderator and self.request.user.cnt_getvote > 800:
            onehrago = datetime.now() - timedelta(hours=1)
            mynews = Post.objects.filter(creation_date__gte=onehrago, type=Post.NEWS, author=request.user).count()
            if mynews > 5:
                self.template_name = "single_message.html"
                msg = "对不起，为保证质量，非管理员一小时内只可以发布一条新闻话题"
                icon = "fa fa-smile-o"
                return render(request, self.template_name, {'msg': msg, 'icon': icon}) 

        # Attempt to prefill from GET parameters
        for key in "title tag_val content".split():
            value = request.GET.get(key)
            if value:
                initial[key] = value


        # Attempt to prefill from external session
        sess = request.session
        if settings.EXTERNAL_SESSION_KEY in sess:
            for field in settings.EXTERNAL_SESSION_FIELDS:
                initial[field] = sess[settings.EXTERNAL_SESSION_KEY].get(field)
            del sess[settings.EXTERNAL_SESSION_KEY]

        form = self.form_class(initial=initial)
        return render(request, self.template_name, {'form': form, 'form_err':''})


    def post(self, request, *args, **kwargs):
        logger.info("%s user %s new post news %s" % (get_ip(request), request.user, kwargs))



        title = self.request.POST.get('title', 0)
        content = self.request.POST.get('content', 0)       
        post_type = Post.NEWS
        tag_val = "新闻"
        status = Post.OPEN
        create_draft = int(self.request.GET.get('d', 0))
        mainpost = None



        if len(content) < 50 or len(content) > 200000:
            print( len(content))
            ret={'r':0, 'm': '字数不能少于50字或大于200000字'}
            return HttpResponse(json.dumps(ret), content_type = "application/json")

        
        tag_val = tag_val.replace(',', ' ')
        # Create a new post.
        if create_draft:#if frontend says it's draft, save draft style 
            post = Post(
                title=title, content=content, author=request.user, type=post_type,
                tag_val=tag_val, status=Post.DRAFT,
            )
            post.save()
            
        else:
            post = Post(
                title=title, content=content, author=request.user, type=post_type,
                tag_val=tag_val, 
            )
            post.save(update=True)

        post.mainpost_id = post.id
        post.root_id = post.id
        post.isfront = True
        post.save()
        

        if post.status == Post.OPEN:
            if post.type == Post.ANSWER:
                stub_update_user_answer_count(request.user)
            elif post.type == Post.COMMENT:
                request.user.cnt_cmt += 1
            request.user.save()

        post.add_tags(post.tag_val)


        post.is_editable = True #editable for user who created it
        post.has_bookmark = False
        post.has_upvote = False
        post.can_accept = False
 

        if (post.type == Post.BLOG) and post.status == Post.OPEN:
            celery.notify_imgserver.delay(post.id)
        if post.status == Post.OPEN:
            celery.send_mentionuser_msg.delay(request.user, post, content, post.type)


        #pid: parent id, id: post id
        ret={'r':1, 'm': '', 'id': post.id,     
             'p_thumb': settings.UPLOAD_IMG_STATIC_FOLDER + "small50-"+post.author.profile.thumbnail,
             'p_h': render_to_string("post_single_answer.html", dict(post=post, user=request.user))}
        return HttpResponse(json.dumps(ret), content_type = "application/json")

class EditColumn(LoginRequiredMixin, FormView):
    """
    Edits an existing post.
    """

    # The template_name attribute must be specified in the calling apps.
    template_name = "post_edit.html"
    form_class = LongForm

    def get(self, request, *args, **kwargs):
        logger.info("%s user %s edit post get %s" % (get_ip(request), request.user, kwargs))
        initial = {}

        pk = int(self.kwargs['pk'])
        post = Post.objects.get(pk=pk)
        post = auth.post_permissions(request=request, post=post)

        # Check and exit if not a valid edit.
        if not post.is_editable:
            logger.error("%s user %s editpost no auth %s" % (get_ip(request), request.user, kwargs))
            return HttpResponseRedirect(reverse("home"))

        initial = dict(title=post.title, content=post.content, post_type=post.type, tag_val=post.tag_val)

        # Disable rich editing for preformatted posts
        pre = 'class="preformatted"' in post.content
        #form_class = LongForm if post.is_toplevel else ShortForm
        #form = form_class(initial=initial)
        return render(request, self.template_name, {'post': post, 'pre': pre})

    def post(self, request, *args, **kwargs):
        logger.info("%s user %s edit ans post %s" % (get_ip(request), request.user, kwargs))
        #import pdb
        #pdb.set_trace()
        #todo!! comment cannot be update

        pk = int(self.kwargs['pk'])
        title = self.request.POST.get('title', 0)
        content = self.request.POST.get('content', 0)       
        post_type = Post.BLOG
        tag_val = self.request.POST.get('tag_val', 0)
        status = Post.TOOPEN
        edit_draft = int(self.request.GET.get('d', 0))
        mainpost = None



        if len(content) < 50 or len(content) > 100000:
            ret={'r':0, 'm': '字数不能少于50字或大于100000字'}
            return HttpResponse(json.dumps(ret), content_type = "application/json")

        try:
            post = Post.objects.get(pk=pk)
        except ObjectDoesNotExist, exc:
            logger.error("%s user %s editpost not exist %s" % (get_ip(request), request.user, kwargs))
            ret={'r':0, 'm': '该帖子不存在，可能已经被删除'}
            return HttpResponse(json.dumps(ret), content_type = "application/json")

        pre_status = post.status
        post = auth.post_permissions(request=request, post=post)

        user = request.user

        # For historical reasons we had posts with iframes
        # these cannot be edited because the content would be lost in the front end
        # if "<iframe" in post.content:
        #     messages.error(request, "This post is not editable because of an iframe! Contact if you must edit it")
        #     ret={'r':0, 'm': '非法操作：包含iframe'}
        #     return HttpResponse(json.dumps(ret), content_type = "application/json")

        # Check and exit if not a valid edit.
        if not post.is_editable:
            logger.error("%s user %s editpost no auth %s" % (get_ip(request), request.user, kwargs))
            ret={'r':0, 'm': '你无权操作'}
            return HttpResponse(json.dumps(ret), content_type = "application/json")

        tag_val = tag_val.replace(',', ' ')
        if edit_draft: #if edit draft, keep status as DRAFT.  
            if post.status==Post.OPEN:
                post.status = Post.DRAFT
            #else: #if pending draft hidden, keep as is
            # TODO: fix this oversight!
            post.content = content
            post.status = post.status #otherwise it will set to PENDING
            post.tag_val = tag_val

            # This is needed to validate some fields.
            post.save(update=False)
        else:
            #if post status is OPEN or DRAFT, set as OPEN. leave other status as is.
            if post.status == Post.OPEN or post.status == Post.DRAFT:
                post.status = Post.OPEN 
            elif post.status == Post.HIDDEN:
                post.status = Post.PENDING
            # TODO: fix this oversight!
            post.content = content
            post.status = post.status #otherwise it will set to PENDING
            post.tag_val = tag_val

            # This is needed to validate some fields.
            post.save(update=False) #edit: means already update stats before
       

        
        post.add_tags(tag_val)

        # Update the last edit user.
        post.lastedit_user = request.user
        post.status = post.status

        stub_update_reply_count(post)

        post.save(update=False)
 

        #fetch image 
        if (post.type == Post.BLOG) and post.status == Post.OPEN:
            celery.notify_imgserver.delay(post.id)


        ret={'r':1, 'm': '', 'p_h':post.html}
        return HttpResponse(json.dumps(ret), content_type = "application/json")

    def get_success_url(self):
        return reverse("user_details", kwargs=dict(pk=self.kwargs['pk']))




class EditNews(LoginRequiredMixin, FormView):
    """
    Edits an existing post.
    """

    # The template_name attribute must be specified in the calling apps.
    template_name = "post_edit.html"
    form_class = LongForm

    def get(self, request, *args, **kwargs):
        logger.info("%s user %s edit post get %s" % (get_ip(request), request.user, kwargs))
        initial = {}

        pk = int(self.kwargs['pk'])
        post = Post.objects.get(pk=pk)
        post = auth.post_permissions(request=request, post=post)

        # Check and exit if not a valid edit.
        if not post.is_editable:
            logger.error("%s user %s editpost no auth %s" % (get_ip(request), request.user, kwargs))
            return HttpResponseRedirect(reverse("home"))

        initial = dict(title=post.title, content=post.content, post_type=post.type, tag_val=post.tag_val)

        # Disable rich editing for preformatted posts
        pre = 'class="preformatted"' in post.content
        #form_class = LongForm if post.is_toplevel else ShortForm
        #form = form_class(initial=initial)
        return render(request, self.template_name, {'post': post, 'pre': pre, 'user': request.user})

    def post(self, request, *args, **kwargs):
        logger.info("%s user %s edit ans post %s" % (get_ip(request), request.user, kwargs))
        #import pdb
        #pdb.set_trace()
        #todo!! comment cannot be update

        pk = int(self.kwargs['pk'])
        title = self.request.POST.get('title', 0)
        content = self.request.POST.get('content', 0)       
        post_type = Post.NEWS
        tag_val = "新闻"
        status = Post.TOOPEN
        edit_draft = int(self.request.GET.get('d', 0))
        mainpost = None



        if len(content) < 50 or len(content) > 100000:
            ret={'r':0, 'm': '字数不能少于50字或大于100000字'}
            return HttpResponse(json.dumps(ret), content_type = "application/json")

        try:
            post = Post.objects.get(pk=pk)
        except ObjectDoesNotExist, exc:
            logger.error("%s user %s editpost not exist %s" % (get_ip(request), request.user, kwargs))
            ret={'r':0, 'm': '该帖子不存在，可能已经被删除'}
            return HttpResponse(json.dumps(ret), content_type = "application/json")

        pre_status = post.status
        post = auth.post_permissions(request=request, post=post)

        user = request.user

        # For historical reasons we had posts with iframes
        # these cannot be edited because the content would be lost in the front end
        # if "<iframe" in post.content:
        #     messages.error(request, "This post is not editable because of an iframe! Contact if you must edit it")
        #     ret={'r':0, 'm': '非法操作：包含iframe'}
        #     return HttpResponse(json.dumps(ret), content_type = "application/json")

        # Check and exit if not a valid edit.
        if not post.is_editable:
            logger.error("%s user %s editpost no auth %s" % (get_ip(request), request.user, kwargs))
            ret={'r':0, 'm': '你无权操作'}
            return HttpResponse(json.dumps(ret), content_type = "application/json")

        tag_val = tag_val.replace(',', ' ')
        if edit_draft: #if edit draft, keep status as DRAFT.  
            if post.status==Post.OPEN:
                post.status = Post.DRAFT
            #else: #if pending draft hidden, keep as is
            # TODO: fix this oversight!
            post.content = content
            post.status = post.status #otherwise it will set to PENDING
            post.tag_val = tag_val

            # This is needed to validate some fields.
            post.save(update=False)
        else:
            #if post status is OPEN or DRAFT, set as OPEN. leave other status as is.
            if post.status == Post.OPEN or post.status == Post.DRAFT:
                post.status = Post.OPEN 
            elif post.status == Post.HIDDEN:
                post.status = Post.PENDING
            # TODO: fix this oversight!
            post.content = content
            post.status = post.status #otherwise it will set to PENDING
            post.tag_val = tag_val

            # This is needed to validate some fields.
            post.save(update=False) #edit: means already update stats before
       

        
        post.add_tags(tag_val)

        # Update the last edit user.
        post.lastedit_user = request.user
        post.status = post.status
        post.isfront = True

        stub_update_reply_count(post)

        post.save(update=False)
 

        #fetch image 
        if (post.type == Post.NEWS) and post.status == Post.OPEN:
            celery.notify_imgserver.delay(post.id)


        ret={'r':1, 'm': '', 'p_h':post.html}
        return HttpResponse(json.dumps(ret), content_type = "application/json")

    def get_success_url(self):
        return reverse("user_details", kwargs=dict(pk=self.kwargs['pk']))
