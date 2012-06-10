# -*- encoding: utf-8 -*-
import datetime

from django.views import generic as generic_views
from django.views.generic.base import TemplateResponseMixin
from django.shortcuts import get_object_or_404
from django.http import HttpResponseForbidden, HttpResponseRedirect
from django.contrib import messages
from django.utils.translation import ugettext_lazy as _
from django.core.urlresolvers import reverse
from django.utils.decorators import method_decorator
from django.contrib.auth.decorators import login_required
from django.utils.importlib import import_module

from . import models, forms, utils, decorators, settings
from .view_mixins import OrderMappingMixin, PrepareViewMixin
from pyconde.proposals.views import NextRedirectMixin


class ListProposalsView(OrderMappingMixin, generic_views.TemplateView):
    """
    Lists all proposals the reviewer should be able to review, sorted by
    "priority".

    This listing should include some filter functionality to filter proposals
    by tag, track or kind.

    Access: review-team and staff
    """
    template_name = 'reviews/reviewable_proposals.html'
    order_mapping = {
        'comments': 'num_comments',
        'reviews': 'num_reviews',
        'title': 'proposal__title',
        'activity': 'latest_activity_date',
        'score': 'score',
    }
    default_order = 'reviews'

    def get_context_data(self, **kwargs):
        proposals = self.get_queryset()
        my_reviews = models.Review.objects.filter(user=self.request.user).select_related('proposal', 'proposal.track')
        reviewed_proposals = [rev.proposal for rev in my_reviews]
        for proposal in proposals:
            proposal.reviewed = proposal.proposal in reviewed_proposals
        return {
            'proposals': proposals,
            'order': self.get_request_order(),
            'filter_form': self.filter_form,
        }

    def get_request_order(self):
        """
        Overrides get_request_order to prevent non-authorized users to
        use the score ordering.
        """
        order = super(ListProposalsView, self).get_request_order()
        if order.lstrip('-') == 'score' and not (self.request.user.is_staff or self.request.user.is_superuser):
            return self.get_default_order()
        return order

    def get_queryset(self):
        qs = models.ProposalMetaData.objects.select_related().order_by(self.get_order()).all()
        if self.filter_form.is_valid():
            track_slug = self.filter_form.cleaned_data['track']
            if track_slug:
                qs = qs.filter(proposal__track__slug=track_slug)
        return qs

    @method_decorator(decorators.reviewer_or_staff_required)
    def dispatch(self, request, *args, **kwargs):
        self.filter_form = forms.ProposalFilterForm(request.GET)
        return super(ListProposalsView, self).dispatch(request, *args, **kwargs)


class ListMyProposalsView(ListProposalsView):
    """
    A simple view that allows a user to see the discussions going on around
    his/her proposal.
    """
    template_name = 'reviews/my_proposals.html'
    order_mapping = {
        'comments': 'num_comments',
        'title': 'proposal__title',
        'activity': 'latest_comment_date',
    }
    default_order = '-activity'

    @method_decorator(login_required)
    def dispatch(self, request, *args, **kwargs):
        return super(ListProposalsView, self).dispatch(request, *args, **kwargs)

    def get_queryset(self):
        speaker = self.request.user.speaker_profile
        my_proposals = models.Proposal.objects.filter(speaker=speaker) | models.Proposal.objects.filter(additional_speakers=speaker)
        return models.ProposalMetaData.objects.select_related().filter(proposal__in=my_proposals).order_by(self.get_order()).all()


class MyReviewsView(OrderMappingMixin, generic_views.ListView):
    """
    Lists all the reviews made by the current user.
    """
    model = models.Review
    default_order = 'title'
    order_mapping = {
        'title': 'proposal__title',
        'speaker': 'proposal__speaker',
        'rating': 'rating',
    }

    def get_queryset(self):
        return self.model.objects.filter(user=self.request.user).order_by(self.get_order()).select_related('proposal')

    def get_template_names(self):
        return ['reviews/my_reviews.html']

    def get_context_data(self, *args, **kwargs):
        data = super(MyReviewsView, self).get_context_data(*args, **kwargs)
        data['order'] = self.get_request_order()
        return data

    @method_decorator(decorators.reviewer_required)
    def dispatch(self, request, *args, **kwargs):
        return super(MyReviewsView, self).dispatch(request, *args, **kwargs)


class SubmitReviewView(generic_views.TemplateView):
    """
    Only reviewers should be able to submit reviews as long as the proposal
    accepts one. The review-period should perhaps coincide with closing
    the proposal for discussion.

    Access: review-team
    """
    # TODO: Freeze the proposal version from GET -> POST
    template_name = 'reviews/submit_review_form.html'
    form = None

    def post(self, request, *args, **kwargs):
        self.form = forms.ReviewForm(data=request.POST)
        if self.form.is_valid():
            review = self.form.save(commit=False)
            review.proposal = self.proposal
            review.proposal_version = self.proposal_version
            review.user = request.user
            review.save()
            messages.success(request, "Bewertung gespeichert")
            return HttpResponseRedirect(reverse('reviews-proposal-details', kwargs={'pk': self.proposal.pk}))
        return self.get(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        if self.form is None:
            self.form = forms.ReviewForm()
        return {
            'form': self.form,
            'proposal': self.proposal,
        }

    @method_decorator(decorators.reviewer_required)
    @method_decorator(decorators.reviews_active_required)
    def dispatch(self, request, *args, **kwargs):
        self.proposal = get_object_or_404(models.Proposal, pk=kwargs['pk'])
        if not self.proposal.can_be_reviewed():
            messages.error(request, u"Dieses Proposal kann nicht mehr bewertet werden.")
            return HttpResponseRedirect(reverse('reviews-proposal-details', kwargs={'pk': self.proposal.pk}))
        if models.Review.objects.filter(user=request.user, proposal=self.proposal).count():
            messages.info(request, "Sie haben diesen Vorschlag bereits bewertet")
            return HttpResponseRedirect(reverse('reviews-proposal-details', kwargs={'pk': self.proposal.pk}))
        self.proposal_version = models.ProposalVersion.objects.get_latest_for(self.proposal)
        return super(SubmitReviewView, self).dispatch(request, *args, **kwargs)


class UpdateReviewView(generic_views.UpdateView):
    """
    Allows a reviewer to update his/her review as long as the review period is
    open.

    Access: author of review
    """
    model = models.Review
    template_name_suffix = '_update_form'
    form_class = forms.UpdateReviewForm

    def get_object(self):
        if hasattr(self, 'object') and self.object:
            return self.object
        return self.model.objects.get(user=self.request.user, proposal__pk=self.kwargs['pk'])

    def form_valid(self, form):
        obj = form.save(commit=False)
        obj.proposal_version = models.ProposalVersion.objects.get_latest_for(self.object.proposal)
        obj.save()
        messages.success(self.request, u"Änderungen gespeichert")
        return HttpResponseRedirect(self.get_success_url())

    def get_context_data(self, **kwargs):
        data = super(UpdateReviewView, self).get_context_data(**kwargs)
        data['proposal'] = self.object.proposal
        return data

    def get_success_url(self):
        return reverse('reviews-proposal-details', kwargs={'pk': self.kwargs['pk']})

    @method_decorator(decorators.reviewer_required)
    @method_decorator(decorators.reviews_active_required)
    def dispatch(self, request, *args, **kwargs):
        self.request = request
        self.kwargs = kwargs
        self.object = self.get_object()
        if not self.object.proposal.can_be_reviewed():
            messages.error(request, u"Dieses Proposal kann nicht mehr bewertet werden.")
            return HttpResponseRedirect(reverse('reviews-proposal-details', kwargs={'pk': self.object.proposal.pk}))
        return super(UpdateReviewView, self).dispatch(request, *args, **kwargs)


class DeleteReviewView(NextRedirectMixin, PrepareViewMixin, generic_views.DeleteView):
    """
    Allows the author of a review to delete it.

    Access: Author of review
    """
    model = models.Review

    def get_object(self):
        if hasattr(self, 'object') and self.object:
            return self.object
        return self.model.objects.get(user=self.request.user, proposal__pk=self.kwargs['pk'])

    def get_success_url(self):
        next = self.get_next_redirect()
        if next:
            return next
        return reverse('reviews-proposal-details', kwargs={'pk': self.kwargs['pk']})

    def get_context_data(self, *args, **kwargs):
        data = super(DeleteReviewView, self).get_context_data(*args, **kwargs)
        data['next'] = self.get_success_url()
        return data

    def delete(self, request, *args, **kwargs):
        resp = super(DeleteReviewView, self).delete(request, *args, **kwargs)
        messages.success(request, u"Review wurde gelöscht")
        return resp

    @method_decorator(decorators.reviews_active_required)
    @method_decorator(login_required)
    def dispatch(self, request, *args, **kwargs):
        self.prepare(request, *args, **kwargs)
        if not self.object.proposal.can_be_reviewed():
            messages.error(request, u"Dieses Proposal kann nicht mehr bewertet werden.")
            return HttpResponseRedirect(reverse('reviews-proposal-details', kwargs={'pk': self.object.proposal.pk}))
        return super(DeleteReviewView, self).dispatch(request, *args, **kwargs)


class SubmitCommentView(TemplateResponseMixin, generic_views.View):
    """
    Everyone on the review-team as well as the original author should be
    able to comment on a proposal.

    Access: speaker, co-speakers and review-team
    """
    template_name = 'reviews/comment_form.html'

    def get(self, request, *args, **kwargs):
        if not hasattr(self, 'form') or self.form is None:
            self.form = forms.CommentForm()
        return self.render_to_response({
            'form': self.form
            })

    def post(self, request, *args, **kwargs):
        self.form = forms.CommentForm(data=request.POST)
        if self.form.is_valid():
            comment = self.form.save(commit=False)
            comment.author = request.user
            comment.proposal = self.proposal
            comment.proposal_version = self.proposal_version
            comment.save()
            messages.success(request, _("Comment added"))
            if settings.ENABLE_COMMENT_NOTIFICATIONS:
                utils.send_comment_notification(comment)
            return HttpResponseRedirect(reverse('reviews-proposal-details', kwargs={'pk': self.proposal.pk}))
        return self.get(request, *args, **kwargs)

    @method_decorator(decorators.reviews_active_required)
    @method_decorator(login_required)
    def dispatch(self, request, *args, **kwargs):
        self.proposal = get_object_or_404(models.Proposal, pk=kwargs['pk'])
        if not utils.can_participate_in_review(request.user, self.proposal):
            return HttpResponseForbidden()
        self.proposal_version = models.ProposalVersion.objects.get_latest_for(self.proposal)
        return super(SubmitCommentView, self).dispatch(request, *args, **kwargs)


class DeleteCommentView(NextRedirectMixin, PrepareViewMixin, generic_views.DeleteView):
    """
    Allows the author of a comment or a staff member to delete a review (i.e.
    mark it as deleted).

    Access: Author of comment and staff member
    """
    model = models.Comment

    def get_success_url(self):
        next = self.get_next_redirect()
        if next:
            return next
        return reverse('reviews-proposal-details', kwargs={'pk': self.kwargs['proposal_pk']})

    def get_object(self, **kwargs):
        if hasattr(self, 'object') and self.object:
            return self.object
        return self.model.objects.get(pk=self.kwargs['pk'], proposal__pk=self.kwargs['proposal_pk'])

    def delete(self, *args, **kwargs):
        self.object.mark_as_deleted(self.request.user)
        self.object.save()
        return HttpResponseRedirect(self.get_success_url())

    def get_context_data(self, *args, **kwargs):
        data = super(DeleteCommentView, self).get_context_data(*args, **kwargs)
        data['next'] = self.get_success_url()
        return data

    @method_decorator(decorators.reviews_active_required)
    @method_decorator(login_required)
    def dispatch(self, request, *args, **kwargs):
        self.prepare(request, *args, **kwargs)
        if not (self.object.author == self.request.user or self.request.user.is_staff or self.request.user.is_superuser):
            return HttpResponseForbidden()
        return super(DeleteCommentView, self).dispatch(request, *args, **kwargs)


class ProposalDetailsView(generic_views.DetailView):
    """
    An extended version of the details view of the proposals app that
    also includes the discussion as well as the review of the current user.

    Access: author, review-team and staff (for easier comment moderation)
    Template: reviews/proposal_details.html
    """
    model = models.Proposal

    def get_template_names(self):
        return ['reviews/proposal_details.html']

    def get_context_data(self, **kwargs):
        comment_form = forms.CommentForm()
        comment_form.helper.form_action = reverse('reviews-submit-comment', kwargs={'pk': self.object.pk})
        comments = self.object.comments.select_related('proposal_version', 'author').all()
        proposal_versions = self.object.versions.select_related('creator').all()
        data = super(ProposalDetailsView, self).get_context_data(**kwargs)
        data['comments'] = comments
        data['proposal_version'] = models.ProposalVersion.objects.get_latest_for(self.object)
        data['comment_form'] = comment_form
        data['versions'] = proposal_versions
        data['timeline'] = map(self.wrap_timeline_elements, utils.merge_comments_and_versions(comments, proposal_versions))
        data['can_review'] = utils.can_review_proposal(self.request.user, self.object)
        data['can_update'] = utils.is_proposal_author(self.request.user, self.object)
        data['can_comment'] = utils.can_participate_in_review(self.request.user, self.object)
        try:
            review = self.object.reviews.get(user=self.request.user)
            data['user_review'] = review
            data['review_outdated'] = review.proposal_version != data['proposal_version']
        except:
            pass
        return data

    def get_object(self, queryset=None):
        if hasattr(self, 'object') and self.object:
            return self.object
        return super(ProposalDetailsView, self).get_object(queryset)

    @method_decorator(login_required)
    def dispatch(self, request, *args, **kwargs):
        self.request = request
        self.args = args
        self.kwargs = kwargs
        self.object = self.get_object()
        if not (utils.can_participate_in_review(self.request.user, self.object) or request.user.is_staff):
            return HttpResponseForbidden()
        return super(ProposalDetailsView, self).dispatch(request, *args, **kwargs)

    def wrap_timeline_elements(self, item):
        type_ = 'comment'
        if isinstance(item, models.ProposalVersion):
            type_ = 'version'
        return {
            'type': type_,
            'item': item
        }


class ProposalVersionListView(generic_views.ListView):
    """
    Lists all versions of a given proposal.

    Access: author of proposal and reviewer and staff
    """
    model = models.ProposalVersion
    proposal = None

    def get_queryset(self):
        if self.proposal is None:
            self.proposal = models.Proposal.objects.get(pk=self.kwargs['proposal_pk'])
        return self.model.objects.filter(original=self.proposal)

    def get_context_data(self, **kwargs):
        data = super(ProposalVersionListView, self).get_context_data(**kwargs)
        data['original'] = models.Proposal.objects.get(pk=self.kwargs['proposal_pk'])
        data['proposal'] = data['original']
        return data

    @method_decorator(decorators.reviews_active_required)
    def get(self, *args, **kwargs):
        self.object_list = self.get_queryset()
        if not (utils.can_participate_in_review(self.request.user, self.proposal) or self.request.user.is_staff):
            return HttpResponseForbidden()
        return super(ProposalVersionListView, self).get(*args, **kwargs)


class ProposalVersionDetailsView(generic_views.DetailView):
    """
    Displays details of a specific proposal version.

    Access: author of proposal and reviewer and staff
    """
    model = models.ProposalVersion
    context_object_name = 'version'

    def get_object(self):
        if hasattr(self, 'object') and self.object:
            return self.object
        return self.model.objects.select_related('original').get(pk=self.kwargs['pk'], original__pk=self.kwargs['proposal_pk'])

    def get_context_data(self, **kwargs):
        data = super(ProposalVersionDetailsView, self).get_context_data(**kwargs)
        proposal = data['version'].original
        data.update({
            'proposal': proposal,
            'versions': proposal.versions.select_related('creator').all()
        })
        return data

    @method_decorator(decorators.reviews_active_required)
    def get(self, request, *args, **kwargs):
        self.object = self.get_object()
        if not (utils.can_participate_in_review(self.request.user, self.object.original) or request.user.is_staff):
            return HttpResponseForbidden()
        return super(ProposalVersionDetailsView, self).get(request, *args, **kwargs)


class UpdateProposalView(TemplateResponseMixin, generic_views.View):
    """
    This should create a new version of the proposal and notify all
    contributors to the discussion so far.

    Access: speaker and co-speakers
    """
    template_name = 'reviews/update_proposal.html'
    form = None

    def get(self, request, *args, **kwargs):
        if self.form is None:
            if self.proposal_version:
                self.form = self.get_form_class().init_from_proposal(self.proposal_version)
            else:
                self.form = self.get_form_class().init_from_proposal(self.object)
        return self.render_to_response({
            'form': self.form,
            'proposal': self.object,
            'proposal_version': self.proposal_version
            })

    def post(self, request, *args, **kwargs):
        self.form = self.get_form_class()(data=request.POST)
        if not self.form.is_valid():
            return self.get(request, *args, **kwargs)
        new_version = self.form.save(commit=False)
        # Set the fields we don't want people to be able to modify right now
        new_version.original = self.object
        new_version.creator = request.user
        new_version.pub_date = datetime.datetime.now()
        new_version.conference = self.object.conference
        new_version.kind = self.object.kind
        new_version.speaker = self.object.speaker
        new_version.submission_date = self.object.submission_date
        new_version.save()
        new_version.additional_speakers = self.object.additional_speakers.all()
        self.form.save_m2m()
        messages.success(request, _("Proposal successfully update"))
        if settings.ENABLE_PROPOSAL_UPDATE_NOTIFICATIONS:
            utils.send_proposal_update_notification(new_version)
        return HttpResponseRedirect(reverse('reviews-proposal-details', kwargs={'pk': self.object.pk}))

    @method_decorator(decorators.reviews_active_required)
    def dispatch(self, request, *args, **kwargs):
        self.object = get_object_or_404(models.Proposal.objects, pk=kwargs['pk'])
        if not self.object.can_be_updated():
            messages.error(request, u"Vorschläge können nicht mehr editiert werden.")
            return HttpResponseRedirect(reverse('reviews-proposal-details', kwargs={'pk': self.object.pk}))
        self.proposal_version = models.ProposalVersion.objects.get_latest_for(self.object)
        if not utils.is_proposal_author(request.user, self.object):
            return HttpResponseForbidden()
        return super(UpdateProposalView, self).dispatch(request, *args, **kwargs)

    def get_form_class(self):
        type_slug = self.object.kind.slug
        formcls_path = settings.PROPOSAL_UPDATE_FORMS.get(type_slug)
        if formcls_path:
            mod_name, cls_name = formcls_path.rsplit('.', 1)
            mod = import_module(mod_name)
            form_cls = getattr(mod, cls_name)
            if form_cls:
                return form_cls
        return forms.UpdateProposalForm

    def get_template_names(self):
        return [
            'reviews/update_{0}_proposal.html'.format(self.object.kind.slug),
            self.template_name
        ]
