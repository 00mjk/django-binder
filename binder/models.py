import re
import warnings
from collections import defaultdict
from datetime import date, datetime, time
from contextlib import suppress

from django.db import models
from django.contrib.postgres.fields import CITextField, ArrayField, JSONField
from django.db.models import signals
from django.core.exceptions import ValidationError
from django.db.models.query_utils import Q
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime

from binder.json import jsonloads

from binder.exceptions import BinderRequestError

from . import history


class CaseInsensitiveCharField(CITextField):
	def __init__(self, *args, **kwargs):
		warnings.warn(DeprecationWarning('CaseInsensitiveCharField is deprecated, use django.contrib.postgres.fields.CITextField instead'))
		return super().__init__(*args, **kwargs)



class UpperCaseCharField(CITextField):
	def get_prep_value(self, value):
		value = super().get_prep_value(value)
		if value is None:
			return None
		return value.upper()



class LowerCaseCharField(CITextField):
	def get_prep_value(self, value):
		value = super().get_prep_value(value)
		if value is None:
			return None
		return value.lower()



class ChoiceEnum(object):
	def __init__(self, *args, **kwargs):
		self.items = kwargs
		for k in args:
			if k == '':
				self.items['NONE'] = ''
			else:
				self.items[re.sub('[ /+-]', '_', k).upper()] = k
		self.__dict__.update(self.items)

	def choices(self):
		return tuple(sorted((v, k) for k, v in self.items.items()))

	def name(self, value, default=None):
		if value is None:
			return default
		for k, v in self.items.items():
			if v == value:
				return k
		raise ValueError()

	def __call__(self, **kwargs):
		return models.CharField(
			choices=self.choices(),
			max_length=max(map(len, self.items.values())),
			**kwargs
		)



class FieldFilter(object):
	# The classes that this filter applies to (should be mutually
	# exclusive with the other classes)
	fields = []
	# The list of allowed qualifiers
	allowed_qualifiers = []

	def __init__(self, field):
		self.field = field



	def field_description(self):
		return '{} {{{}}}.{{{}}}'.format(self.field.__class__.__name__, self.field.model.__name__, self.field.name)



	def clean_value(self, qualifier, v):
		raise ValueError('FieldFilter {} has not overridden the clean_value method'.format(self.__class__.name))



	def check_qualifier(self, qualifier):
		if qualifier not in self.allowed_qualifiers:
			raise BinderRequestError('Qualifier {} not supported for type {} ({}).'
					.format(qualifier, self.__class__.__name__, self.field_description()))



	def get_q(self, qualifier, value, invert, partial=''):
		self.check_qualifier(qualifier)

		# TODO: Try to make the splitting and cleaning more re-usable
		if qualifier in ('in', 'range'):
			values = value.split(',')
			if qualifier == 'range':
				if len(values) != 2:
					raise BinderRequestError('Range requires exactly 2 values for {}.'.format(self.field_description()))
		else:
			values = [value]


		if qualifier == 'isnull':
			cleaned_value = True
		elif qualifier in ('in', 'range'):
			cleaned_value = [self.clean_value(qualifier, v) for v in values]
		else:
			try:
				cleaned_value = self.clean_value(qualifier, values[0])
			except IndexError:
				raise ValidationError('Value for filter {{{}}}.{{{}}} may not be empty.'.format(self.field.model.__name__, self.field.name))

		suffix = '__' + qualifier if qualifier else ''
		if invert:
			return ~Q(**{partial + self.field.name + suffix: cleaned_value})
		else:
			return Q(**{partial + self.field.name + suffix: cleaned_value})



class IntegerFieldFilter(FieldFilter):
	fields = [
		models.IntegerField,
		models.ForeignKey,
		models.AutoField,
		models.ManyToOneRel,
		models.ManyToManyField,
		models.ManyToManyRel,
	]
	allowed_qualifiers = [None, 'in', 'gt', 'gte', 'lt', 'lte', 'range', 'isnull']

	def clean_value(self, qualifier, v):
		try:
			return int(v)
		except ValueError:
			raise ValidationError('Invalid value {{{}}} for {}.'.format(v, self.field_description()))



class FloatFieldFilter(FieldFilter):
	fields = [models.FloatField]
	allowed_qualifiers = [None, 'in', 'gt', 'gte', 'lt', 'lte', 'range', 'isnull']

	def clean_value(self, qualifier, v):
		try:
			return float(v)
		except ValueError:
			raise ValidationError('Invalid value {{{}}} for {}.'.format(v, self.field_description()))



class DateFieldFilter(FieldFilter):
	fields = [models.DateField]
	# Maybe allow __startswith? And __year etc?
	allowed_qualifiers = [None, 'in', 'gt', 'gte', 'lt', 'lte', 'range', 'isnull']

	def clean_value(self, qualifier, v):
		if not re.match('^[0-9]{4}-[0-9]{2}-[0-9]{2}$', v):
			raise ValidationError('Invalid YYYY-MM-DD value {{{}}} for {}.'.format(v, self.field_description()))
		else:
			return parse_date(v)
		return v



class DateTimeFieldFilter(FieldFilter):
	fields = [models.DateTimeField]
	# Maybe allow __startswith? And __year etc?
	allowed_qualifiers = [None, 'in', 'gt', 'gte', 'lt', 'lte', 'range', 'isnull']

	def clean_value(self, qualifier, v):
		if re.match('^[0-9]{4}-[0-9]{2}-[0-9]{2}[T ][0-9]{2}:[0-9]{2}:[0-9]{2}([.][0-9]+)?([A-Za-z]+|[+-][0-9]{1,4})$', v):
			return parse_datetime(v)
		if re.match('^[0-9]{4}-[0-9]{2}-[0-9]{2}$', v):
			return parse_date(v)
		else:
			raise ValidationError('Invalid YYYY-MM-DD(.mmm)ZONE value {{{}}} for {}.'.format(v, self.field_description()))
		return v


	def get_q(self, qualifier, value, invert, partial=''):
		self.check_qualifier(qualifier)

		# TODO: Try to make the splitting and cleaning more re-usable
		if qualifier in ('in', 'range'):
			values = value.split(',')
			if qualifier == 'range':
				if len(values) != 2:
					raise BinderRequestError('Range requires exactly 2 values for {}.'.format(self.field_description()))
		else:
			values = [value]


		if qualifier == 'isnull':
			cleaned_value = True
		elif qualifier in ('in', 'range'):
			cleaned_value = [self.clean_value(qualifier, v) for v in values]
			types = {type(v) for v in cleaned_value}
			if len(types) != 1:
				raise ValidationError('Values for filter {{{}}}.{{{}}} must be the same types.'.format(self.field.model.__name__, self.field.name))
			if isinstance(cleaned_value[0], date) and not isinstance(cleaned_value[0], datetime):
				qualifier = 'date__' + qualifier
		else:
			try:
				cleaned_value = self.clean_value(qualifier, values[0])
				if isinstance(cleaned_value, date) and not isinstance(cleaned_value, datetime):
					qualifier = 'date__' + qualifier if qualifier else 'date'
			except IndexError:
				raise ValidationError('Value for filter {{{}}}.{{{}}} may not be empty.'.format(self.field.model.__name__, self.field.name))

		suffix = '__' + qualifier if qualifier else ''
		if invert:
			return ~Q(**{partial + self.field.name + suffix: cleaned_value})
		else:
			return Q(**{partial + self.field.name + suffix: cleaned_value})



class TimeFieldFilter(FieldFilter):
	fields = [models.TimeField]
	# Maybe allow __startswith? And __year etc?
	allowed_qualifiers = [None, 'in', 'gt', 'gte', 'lt', 'lte', 'range', 'isnull']
	time_re = re.compile(r'^(\d{2}):(\d{2}):(\d{2})(?:\.(\d+))?(Z|[+-]\d{2}(?:\d{2})?)$')

	def clean_value(self, qualifier, v):
		# Match value
		match = self.time_re.match(v)
		if not match:
			raise ValidationError('Invalid HH:MM:SS(.mmm) value {{{}}} for {}.'.format(v, self.field_description()))
		# Get values
		hour, minute, second, microsecond, tzinfo = match.groups()
		hour = int(hour)
		minute = int(minute)
		second = int(second)
		microsecond = int((microsecond or '').ljust(6, '0'))
		if tzinfo == 'Z':
			tzinfo = timezone.utc
		else:
			tzinfo = tzinfo.ljust(5, '0')
			offset = int(tzinfo[1:3]) * 60 + int(tzinfo[3:5])
			if tzinfo.startswith('-'):
				offset = -offset
			tzinfo = timezone.get_fixed_timezone(offset)
		# Create time object
		return time(
			hour=hour,
			minute=minute,
			second=second,
			microsecond=microsecond,
			tzinfo=tzinfo,
		)



class BooleanFieldFilter(FieldFilter):
	fields = [models.BooleanField]
	allowed_qualifiers = [None]

	def clean_value(self, qualifier, v):
		if v == 'true':
			return True
		elif v == 'false':
			return False
		else:
			raise ValidationError('Invalid value {{{}}} for {}.'.format(v, self.field_description()))



class TextFieldFilter(FieldFilter):
	fields = [models.CharField, models.TextField]
	allowed_qualifiers = [None, 'in', 'iexact', 'contains', 'icontains', 'startswith', 'istartswith', 'endswith', 'iendswith', 'exact', 'isnull']

	# Always valid(?)
	def clean_value(self, qualifier, v):
		return v


class UUIDFieldFilter(FieldFilter):
	fields = [models.UUIDField]
	allowed_qualifiers = [None, 'in', 'iexact', 'contains', 'icontains', 'startswith', 'istartswith', 'endswith', 'iendswith', 'exact']

	# Always valid; when using "contains" this doesn't need to be
	# an actually formatted uuid.
	def clean_value(self, qualifier, v):
		return v


class ArrayFieldFilter(FieldFilter):
	fields = [ArrayField]
	allowed_qualifiers = [None, 'contains', 'contained_by', 'overlap', 'isnull']

	# Some copy/pasta involved....
	def get_field_filter(self, field_class, reset=False):
		f = not reset and getattr(self, '_field_filter', None)

		if not f:
			f = None
			for field_filter_cls in FieldFilter.__subclasses__():
				for field_cls in field_filter_cls.fields:
					if field_cls == field_class:
						f = field_filter_cls
						break
			self._field_filter = f

		return f


	def clean_value(self, qualifier, v):
		Filter = self.get_field_filter(self.field.base_field.__class__)
		filter = Filter(self.field.base_field)
		if v == '': # Special case: This should represent the empty array, not an array with one empty string
			return []
		else:
			values = v.split(',')
			return list(map(lambda v: filter.clean_value(qualifier, v), values))


class JSONFieldFilter(FieldFilter):
	fields = [JSONField]
	# TODO: Element or path-based lookup is not supported yet
	allowed_qualifiers = [None, 'contains', 'contained_by', 'has_key', 'has_any_keys', 'has_keys', 'isnull']

	def clean_value(self, qualifier, v):
		if qualifier == 'has_key':
			return v
		elif qualifier in ('has_keys', 'has_any_keys'):
			if v == '':
				return []
			else:
				return v.split(',')
		else:
			# Use bytes to allow decode() to work.  We don't just
			# json.loads because we want to behave identically to
			# any other Binder JSON decode when there are errors.
			return jsonloads(bytes(v, 'utf-8'))



class BinderModelBase(models.base.ModelBase):
	def __new__(cls, name, bases, attrs):
		# Verify that any Foo(BinderModel).Meta descends from BinderModel.Meta. Django messes
		# around with Meta a lot in its metaclass, to the point where we can no longer check this.
		# So we have to inject our own metaclass.__new__ to find this. See #96
		# Bonus points: this way we throw all these warnings at startup.

		# NameError: happens when name='BinderModel' -> ignore
		# KeyError:  happens when Foo doesn't declare Meta -> ignore
		with suppress(NameError, KeyError):
			if not issubclass(attrs['Meta'], BinderModel.Meta):
				warnings.warn(RuntimeWarning('{}.{}.Meta does not descend from BinderModel.Meta'.format(attrs.get('__module__'), name)))
		return super().__new__(cls, name, bases, attrs)



class BinderModel(models.Model, metaclass=BinderModelBase):
	def binder_concrete_fields_as_dict(self, skip_deferred_fields=False):
		fields = {}
		deferred_fields = self.get_deferred_fields()

		for field in [f for f in self._meta.get_fields() if f.concrete and not f.many_to_many]:
			if skip_deferred_fields and field.attname in deferred_fields:
				continue
			elif isinstance(field, models.ForeignKey):
				fields[field.name] = getattr(self, field.name + '_id')
			elif isinstance(field, models.FileField):
				fields[field.name] = str(getattr(self, field.name))
			else:
				fields[field.name] = getattr(self, field.name)
		return fields

	def binder_serialize_m2m_field(self, field):
		if isinstance(field, str):
			field = getattr(self, field)

		try:
			extended_m2m = field.through.binder_is_binder_model
		except AttributeError:
			extended_m2m = False

		# Regular many to many; get a list of the target ids.
		if not extended_m2m:
			return set(field.values_list('id', flat=True))

		# Extended m2m; get dicts of the intermediary join table objects
		data = list(field.through.objects.filter(**{field.source_field.name: self.id}).values())
		# Then, modify them to leave out the PKs and source ids. Also, rename target ids to 'id'.
		for d in data:
			d.pop('id')
			d.pop(field.source_field.name + '_id')
			d['id'] = d.pop(field.target_field.name + '_id')

		return set(sorted(d.items()) for d in data)

	binder_is_binder_model = True

	class Binder:
		history = False

	class Meta:
		abstract = True
		ordering = ['pk']

	def save(self, *args, **kwargs):
		self.full_clean() # Never allow saving invalid models!
		return super().save(*args, **kwargs)


	# This can be overridden in your model when there are special
	# validation rules like partial indexes that may need to be
	# recomputed when other fields change.
	def field_requires_clean_validation(self, field):
		return self.field_changed(field)


	def full_clean(self, exclude=None, *args, **kwargs):
		# Determine if the field needs an extra nullability check.
		# Expects the field object (not the field name)
		def field_needs_nullability_check(field):
			if isinstance(field, (models.CharField, models.TextField, models.BooleanField)):
				if field.blank and not field.null:
					return True

			return False


		# Gather unchanged fields if LoadedValues mixin available, to
		# avoid querying uniqueness constraints for unchanged
		# relations (an useful performance optimization).
		if hasattr(self, 'field_changed'):
			exclude = set(exclude) if exclude else set()
			for f in self.binder_concrete_fields_as_dict(skip_deferred_fields=True):
				if not self.field_requires_clean_validation(f):
					exclude.add(f)

		validation_errors = defaultdict(list)

		try:
			res = super().full_clean(exclude=exclude, *args, **kwargs)
		except ValidationError as ve:
			if hasattr(ve, 'error_dict'):
				for key, value in ve.error_dict.items():
					validation_errors[key] += value
			elif hasattr(ve, 'error_list'):
				for e in ve.error_list:
					validation_errors['null'].append(e) # XXX

		# Django's standard full_clean() doesn't complain about some
		# not-NULL fields being None.  This causes save() to explode
		# with a django.db.IntegrityError because the column is NOT
		# NULL. Tyvm, Django.  So we perform an extra NULL check for
		# some cases. See #66, T2989, T9646.
		for f in self._meta.fields:
			if field_needs_nullability_check(f):
				# gettattr on a foreignkey foo gets the related model, while foo_id just gets the id.
				# We don't need or want the model (nor the DB query), we'll take the id thankyouverymuch.
				name = f.name + ('_id' if isinstance(f, models.ForeignKey) else '')

				if getattr(self, name) is None and getattr(self, f.name) is None:
					validation_errors[f.name].append(ValidationError(
						'This field cannot be null.',
						code='null',
					))

		if validation_errors:
			raise ValidationError(validation_errors)
		else:
			return res


def history_obj_post_init(sender, instance, **kwargs):
	instance._history = instance.binder_concrete_fields_as_dict(skip_deferred_fields=True)

	if not instance.pk:
		instance._history = {k: history.NewInstanceField for k in instance._history}



def history_obj_post_save(sender, instance, **kwargs):
	for field_name, new_value in instance.binder_concrete_fields_as_dict().items():
		try:
			old_value = instance._history[field_name]
			if old_value != new_value:
				history.change(sender, instance.pk, field_name, old_value, new_value)
				instance._history[field_name] = new_value
		except KeyError:
			# Unfetched field (using only(...)), we don't know if it's
			# been changed...
			pass



def history_obj_post_delete(sender, instance, **kwargs):
	history.change(sender, instance.pk, 'pk', instance.pk, None)



def history_obj_m2m_changed(sender, instance, action, reverse, model, pk_set, **kwargs):
	if reverse or action not in ('pre_add', 'pre_remove', 'pre_clear'):
		return

	# Find the corresponding field on the instance
	field = [f for f in instance._meta.get_fields() if f.concrete and f.many_to_many and f.remote_field.through == sender][0]

	history.change(instance.__class__, instance.id, field.name, history.DeferredM2M, history.DeferredM2M)



# FIXME: remove
def install_m2m_signal_handlers(model):
	warnings.warn(DeprecationWarning('install_m2m_signal_handlers() is deprecated, call install_history_signal_handlers() instead!'))
	install_history_signal_handlers(model)



def install_history_signal_handlers(model):
	if model is None:
		return

	if not model.Meta.abstract and model.Binder.history:
		signals.post_init.connect(history_obj_post_init, model)
		signals.post_save.connect(history_obj_post_save, model)
		signals.post_delete.connect(history_obj_post_delete, model)

		for field in model._meta.get_fields():
			if field.many_to_many and field.concrete:
				signals.m2m_changed.connect(history_obj_m2m_changed, getattr(model, field.name).through)

	for sub in model.__subclasses__():
		install_history_signal_handlers(sub)


class ContextAnnotation:

	def __init__(self, func):
		self._func = func

	def get(self, request):
		return self._func(request)


class OptionalAnnotation:

	def __init__(self, expr):
		self._expr = expr

	def get(self, request):
		if isinstance(self._expr, ContextAnnotation):
			return self._expr.get(request)
		else:
			return self._expr
