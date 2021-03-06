from binder.views import ModelView

from ..models import Animal

# From the api docs
class AnimalView(ModelView):
	model = Animal
	m2m_fields = ['costume']
	searches = ['name__icontains']
	transformed_searches = {'zoo_id': int}
