from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path, re_path
from django.views.static import serve

urlpatterns = [
    path('admin/', admin.site.urls),
    path('', include('violations.urls')),
]

# Serve uploaded media files. 
# Using django.views.static.serve allows the app to serve media even in 
# production (DEBUG=False), which is necessary when using Cloudflare Tunnel 
# directly to the container without a separate Nginx setup.
urlpatterns += [
    re_path(r'^media/(?P<path>.*)$', serve, {'document_root': settings.MEDIA_ROOT}),
]

# Standard static files (only for DEBUG)
if settings.DEBUG:
    urlpatterns += static(settings.STATIC_URL, document_root=settings.STATIC_ROOT)
