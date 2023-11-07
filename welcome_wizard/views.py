"""Views for Welcome Wizard."""
from django import forms
from django.conf import settings
from django.contrib import messages
from django.http import HttpResponseForbidden
from django.shortcuts import redirect, render
from django.views.generic import View
from nautobot.circuits.models import CircuitType, Provider
from nautobot.core.utils.permissions import get_permission_for_model
from nautobot.core.views import generic
from nautobot.core.views.mixins import ObjectPermissionRequiredMixin
from nautobot.dcim.models import DeviceType, Location, Manufacturer
from nautobot.extras.datasources import enqueue_pull_git_repository_and_refresh_data
from nautobot.extras.models import GitRepository, Job, JobResult
from nautobot.ipam.models import RIR
from nautobot.virtualization.models import ClusterType

from welcome_wizard.filters import DeviceTypeImportFilterSet, ManufacturerImportFilterSet
from welcome_wizard.forms import (
    DeviceTypeBulkImportForm,
    DeviceTypeImportFilterForm,
    ManufacturerBulkImportForm,
    ManufacturerImportFilterForm,
)
from welcome_wizard.models.importer import DeviceTypeImport, ManufacturerImport
from welcome_wizard.models.merlin import Merlin
from welcome_wizard.tables import DashboardTable, DeviceTypeWizardTable, ManufacturerWizardTable


def check_sync(instance, request):
    """If Device Type Library is enabled and no data in queryset, run the sync."""
    if settings.PLUGINS_CONFIG["welcome_wizard"].get("enable_devicetype-library") and not instance.queryset.exists():
        try:
            repo = GitRepository.objects.get(slug="devicetype_library")
        except GitRepository.DoesNotExist:
            repo = GitRepository(
                name="Devicetype-library",
                slug="devicetype_library",
                remote_url="https://github.com/netbox-community/devicetype-library.git",
                provided_contents=[
                    "welcome_wizard.import_wizard",
                ],
                branch="master",
            )
            repo.save()

        enqueue_pull_git_repository_and_refresh_data(repo, request.user)


class ManufacturerListView(generic.ObjectListView):
    """Table of all Manufacturers discovered in the Git Repository."""

    permission_required = "welcome_wizard.view_manufacturerimport"
    table = ManufacturerWizardTable
    queryset = ManufacturerImport.objects.all()
    action_buttons = ()
    template_name = "welcome_wizard/manufacturer.html"
    filterset = ManufacturerImportFilterSet
    filterset_form = ManufacturerImportFilterForm

    def get(self, request, *args, **kwargs):
        """Add Check Sync to Get."""
        check_sync(instance=self, request=request)
        return super().get(request, *args, **kwargs)


class DeviceTypeListView(generic.ObjectListView):
    """Table of Device Types based on the Manufacturer."""

    permission_required = "welcome_wizard.view_devicetypeimport"
    table = DeviceTypeWizardTable
    queryset = DeviceTypeImport.objects.prefetch_related("manufacturer")
    filterset = DeviceTypeImportFilterSet
    action_buttons = ()
    template_name = "welcome_wizard/devicetype.html"
    filterset_form = DeviceTypeImportFilterForm

    def get(self, request, *args, **kwargs):
        """Add Check Sync to Get."""
        check_sync(instance=self, request=request)
        return super().get(request, *args, **kwargs)


class BulkImportView(View, ObjectPermissionRequiredMixin):
    """Generic Bulk Import View."""

    return_url = None
    model = None
    form = forms.Form
    bulk_import_url = None
    breadcrumb_name = None
    _permission_action = None

    def get_required_permission(self):
        """Return the specific permission necessary to perform the requested action on an object."""
        return get_permission_for_model(self.queryset.model, self._permission_action)

    def dispatch(self, request, *args, **kwargs):
        """Ensures User has permission to add."""
        # Following Nautobot style of adding self._permission_action inside the dispatch.
        self._permission_action = "add"  # pylint disable=attribute-defined-outside-init
        return super().dispatch(request, *args, **kwargs)

    def get(self, request):
        """Single Import Page."""
        # The user is expected to get here only by selecting the Import button from a list view.
        if not request.GET.get("pk"):
            return redirect(self.return_url)
        primarykey = request.GET["pk"]
        initial = {"pk": [primarykey]}

        obj = self.model.objects.get(pk=primarykey)

        form = self.form(initial)

        return render(
            request,
            "welcome_wizard/import.html",
            {
                "form": form,
                "obj": obj,
                "return_url": self.return_url,
                "bulk_import_url": self.bulk_import_url,
                "breadcrumb_name": self.breadcrumb_name,
            },
        )

    def post(self, request):
        """Import Processing."""
        if not self.has_permission():
            return HttpResponseForbidden()
        form = self.form(request.POST)
        if form.is_valid():
            pk_list = request.POST.getlist("pk")
            onboarded = []

            for obj in self.model.objects.filter(pk__in=pk_list):
                if self.model == ManufacturerImport:
                    job = Job.objects.get(name="Welcome Wizard - Import Manufacturer")
                    JobResult.enqueue_job(job_model=job, user=self.request.user, manufacturer_name=obj.name)
                    onboarded.append(obj.name)
                elif self.model == DeviceTypeImport:
                    job = Job.objects.get(name="Welcome Wizard - Import Device Type")
                    JobResult.enqueue_job(job_model=job, user=self.request.user, filename=obj.filename)
                    onboarded.append(obj.name)
            # Currently treat everything as a success...
            messages.success(request, f"Onboarded {len(onboarded)} objects.")

        return redirect(self.return_url)


class ManufacturerBulkImportView(BulkImportView):
    """ManufacturerImport Bulk Import View."""

    model = ManufacturerImport
    form = ManufacturerBulkImportForm
    return_url = "plugins:welcome_wizard:manufacturers"
    bulk_import_url = "plugins:welcome_wizard:manufacturer_import"
    permission_required = "dcim.add_manufacturer"
    queryset = Manufacturer.objects.all()
    breadcrumb_name = "Import Manufacturers"


class DeviceTypeBulkImportView(BulkImportView):
    """DeviceTypeImport Bulk Import View."""

    model = DeviceTypeImport
    form = DeviceTypeBulkImportForm
    return_url = "plugins:welcome_wizard:devicetypes"
    bulk_import_url = "plugins:welcome_wizard:devicetype_import"
    permission_required = "dcim.add_devicetype"
    queryset = DeviceType.objects.prefetch_related("manufacturer")
    breadcrumb_name = "Import Device Types"


class WelcomeWizardDashboard(generic.ObjectListView):
    """Welcome Wizard dashboard view.

    Args:
        View (View): Django View
    """

    action_buttons = ()

    @classmethod
    def check_data(cls):
        """Check data and update the Merlin database."""
        # Check the status of each of the Merlin Items
        # To not have a Merlin import set the `wizard_url` to an empty string `""` so that it will not be processed link
        # wise.
        for nautobot_object, var_name, list_url, new_url, wizard_url in [
            (Location, "Locations", "dcim:location_list", "dcim:location_add", ""),
            (
                Manufacturer,
                "Manufacturers",
                "dcim:manufacturer_list",
                "dcim:manufacturer_add",
                "plugins:welcome_wizard:manufacturer_import",
            ),
            (
                DeviceType,
                "Device Types",
                "dcim:devicetype_list",
                "dcim:devicetype_add",
                "plugins:welcome_wizard:devicetype_import",
            ),
            (
                CircuitType,
                "Circuit Types",
                "circuits:circuittype_list",
                "circuits:circuittype_add",
                "",
            ),
            (
                Provider,
                "Circuit Providers",
                "circuits:provider_list",
                "circuits:provider_add",
                "",
            ),
            (RIR, "RIRs", "ipam:rir_list", "ipam:rir_add", ""),
            (
                ClusterType,
                "VM Cluster Types",
                "virtualization:clustertype_list",
                "virtualization:clustertype_add",
                "",
            ),
        ]:
            completed = nautobot_object.objects.exists()
            try:
                Merlin.objects.filter(name=var_name).update(completed=completed)
                merlin_id = Merlin.objects.get(name=var_name)
            except Merlin.DoesNotExist:
                merlin_id = None

            if merlin_id is None:
                Merlin.objects.create(
                    name=var_name,
                    completed=completed,
                    ignored=False,
                    nautobot_model=nautobot_object,
                    nautobot_add_link=new_url,
                    merlin_link=wizard_url,
                    nautobot_list_link=list_url,
                )

    def get(self, request, *args, **kwargs):
        """Get request."""
        self.check_data()
        return super().get(request, *args, **kwargs)

    def extra_context(self):
        """Override search and table config."""
        return {
            "table_config_form": None,
            "search_form": None,
        }

    permission_required = "welcome_wizard.view_merlin"
    queryset = Merlin.objects.all()
    template_name = "welcome_wizard/dashboard.html"
    table = DashboardTable


class ManufacturerImportDetailView(generic.ObjectView):
    """Detail view for ManufacturerImport."""

    permission_required = "welcome_wizard.view_manufacturerimport"
    queryset = ManufacturerImport.objects.all()


class DeviceTypeImportDetailView(generic.ObjectView):
    """Detail view for DeviceTypeImport."""

    permission_required = "welcome_wizard.view_devicetypeimport"
    queryset = DeviceTypeImport.objects.all()
