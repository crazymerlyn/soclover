from django.urls import path
from . import views

urlpatterns = [
    # Pages
    path('',                        views.home,        name='home'),
    path('room/<str:code>/lobby/',  views.lobby,       name='lobby'),
    path('room/<str:code>/game/',   views.game_view,   name='game_view'),

    # API
    path('api/<str:code>/start/',        views.start_game,  name='start_game'),
    path('api/<str:code>/state/',        views.get_state,   name='get_state'),
    path('api/<str:code>/submit-clues/', views.submit_clues,name='submit_clues'),
    path('api/<str:code>/submit-guess/', views.submit_guess,name='submit_guess'),
    path('api/<str:code>/next-clover/',  views.next_clover, name='next_clover'),
    path('api/<str:code>/kick/<int:player_id>/', views.kick_player, name='kick_player'),
]
