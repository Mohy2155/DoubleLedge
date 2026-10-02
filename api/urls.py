from django.urls import include, path
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView
from rest_framework.routers import DefaultRouter

from api.views import AccountViewSet, JournalEntryViewSet, WebhookEndpointViewSet

router = DefaultRouter()
router.register(r'journal-entries', JournalEntryViewSet, basename='journal-entry')
router.register(r'accounts', AccountViewSet, basename='account')
router.register(r'webhook-endpoints', WebhookEndpointViewSet, basename='webhook-endpoint')

urlpatterns = [
    path('v1/', include(router.urls)),
    path('schema/', SpectacularAPIView.as_view(), name='schema'),
    path('docs/', SpectacularSwaggerView.as_view(url_name='schema'), name='swagger-ui'),
]
