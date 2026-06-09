from django import forms

from .models import Connection


class ConnectionForm(forms.ModelForm):
    class Meta:
        model = Connection
        fields = ["name", "kind", "host", "port", "dbname", "user", "password"]
        widgets = {
            "password": forms.PasswordInput(render_value=True),
        }
