__author__ = 'ialbert'

def post_permissions(request, post):
    """
    Sets permission attributes on a post.

    """
    user = request.user
    is_editable = has_ownership = False

    if user.is_authenticated():

        if user.id == post.author_id:
            has_ownership = is_editable = True
        elif user.is_administrator:
            is_editable = True

    post.is_editable = is_editable
    post.has_ownership = has_ownership

    return post