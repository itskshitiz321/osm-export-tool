# noqa
# -*- coding: utf-8 -*-

import logging
import os
from os.path import join, exists, basename
import json
import shutil
import zipfile
import traceback

import django
from django.apps import apps
from django.conf import settings
from django import db

if not apps.ready and not settings.configured:
    django.setup()

import dramatiq
from raven import Client

from django.utils import timezone
from django.utils.text import get_valid_filename

from jobs.models import Job, HDXExportRegion, PartnerExportRegion
from tasks.models import ExportRun, ExportTask
from hdx_exports.hdx_export_set import slugify, sync_region

import osm_export_tool
import osm_export_tool.tabular as tabular
import osm_export_tool.nontabular as nontabular
from osm_export_tool.mapping import Mapping
from osm_export_tool.geometry import load_geometry
from osm_export_tool.sources import Overpass, OsmiumTool, Galaxy
from osm_export_tool.package import create_package, create_posm_bundle

import shapely.geometry

from .email import (
    send_completion_notification,
    send_error_notification,
    send_hdx_completion_notification,
    send_hdx_error_notification,
)

from .pdc import run_pdc_task

client = Client()

LOG = logging.getLogger(__name__)

ZIP_README = """This thematic file was generated by the HOT Export Tool.
For more information, visit http://export.hotosm.org . 
This theme includes features matching the filter:
{criteria}
clipped to the area defined by the included boundary.geojson.
This theme includes the following OpenStreetMap keys:
{columns}
(c) OpenStreetMap contributors.
This file is made available under the Open Database License: http://opendatacommons.org/licenses/odbl/1.0/. Any rights in individual contents of the database are licensed under the Database Contents License: http://opendatacommons.org/licenses/dbcl/1.0/
"""

class ExportTaskRunner(object):
    def run_task(self, job_uid=None, user=None, ondemand=True): # noqa
        LOG.debug('Running Job with id: {0}'.format(job_uid))
        job = Job.objects.get(uid=job_uid)
        if not user:
            user = job.user
        run = ExportRun.objects.create(job=job, user=user, status='SUBMITTED')
        run.save()
        run_uid = str(run.uid)
        LOG.debug('Saved run with id: {0}'.format(run_uid))

        for format_name in job.export_formats:
            ExportTask.objects.create(
                run=run,
                status='PENDING',
                name=format_name
            )
            LOG.debug('Saved task: {0}'.format(format_name))

        if ondemand:
            # run_task_remote(run_uid)
            # db.close_old_connections()
            run_task_async_ondemand.send(run_uid)
        else:
            run_task_async_scheduled.send(run_uid)
        return run

@dramatiq.actor(max_retries=0,queue_name='default',time_limit=1000*60*60*6) #6 hour
def run_task_async_ondemand(run_uid):
    try:
        run_task_remote(run_uid)
    except:
        pass
    db.close_old_connections()

@dramatiq.actor(max_retries=0,queue_name='scheduled',time_limit=1000*60*60*6)
def run_task_async_scheduled(run_uid):
    try:
        run_task_remote(run_uid)
    except :
        pass
    db.close_old_connections()

def run_task_remote(run_uid):
    try:
        run = ExportRun.objects.get(uid=run_uid)
        run.status = 'RUNNING'
        run.started_at = timezone.now()
        run.save()
        stage_dir = join(settings.EXPORT_STAGING_ROOT, run_uid)
        download_dir = join(settings.EXPORT_DOWNLOAD_ROOT,run_uid)
        if not exists(stage_dir):
            os.makedirs(stage_dir)
        if not exists(download_dir):
            os.makedirs(download_dir)
        run_task(run_uid,run,stage_dir,download_dir)
    except (Job.DoesNotExist,ExportRun.DoesNotExist,ExportTask.DoesNotExist):
        
        LOG.warn('Job was deleted - exiting.')
    except Exception as e:
        client.captureException(extra={'run_uid': run_uid})
        run.status = 'FAILED'
        run.finished_at = timezone.now()
        run.save()

        if HDXExportRegion.objects.filter(job_id=run.job_id).exists():
            send_hdx_error_notification(run, run.job.hdx_export_region_set.first())
        LOG.warn('ExportRun {0} failed: {1}'.format(run_uid, e))
        LOG.warn(traceback.format_exc())
    finally:
        shutil.rmtree(stage_dir)

def run_task(run_uid,run,stage_dir,download_dir):
    LOG.debug('Running ExportRun with id: {0}'.format(run_uid))
    job = run.job
    valid_name = get_valid_filename(job.name)
    

    geom = load_geometry(job.simplified_geom.json)
    export_formats = job.export_formats
    mapping = Mapping(job.feature_selection)

    def start_task(name):
       
        task = ExportTask.objects.get(run__uid=run_uid, name=name)
        task.status = 'RUNNING'
        task.started_at = timezone.now()
        task.save()

    def finish_task(name,created_files=None,response_back=None,planet_file=False):
        # if response_back :
            # print("\nResponse:")
            # print(response_back)
        LOG.debug('Task Finish: {0} for run: {1}'.format(name, run_uid))
        task = ExportTask.objects.get(run__uid=run_uid, name=name)
        task.status = 'SUCCESS'
        task.finished_at = timezone.now()
        # assumes each file only has one part (all are zips or PBFs)
        if response_back :
            # print(response_back)
            task.filenames = [r['download_url'] for r in response_back]
        else:    
            task.filenames = [basename(file.parts[0]) for file in created_files]
        if planet_file is False:
            if response_back:
                total_bytes = 0
                for r in response_back:
                    total_bytes += int(r['zip_file_size_bytes'][0]) #getting filesize bytes
                task.filesize_bytes = total_bytes
            else:
                total_bytes = 0
                for file in created_files:
                    total_bytes += file.size()
                task.filesize_bytes = total_bytes
        task.save()

    is_hdx_export = HDXExportRegion.objects.filter(job_id=run.job_id).exists()
    is_partner_export = PartnerExportRegion.objects.filter(job_id=run.job_id).exists()

    planet_file = False
    polygon_centroid = False
    galaxy_supported_outputs = ['geojson','shp']

    if is_hdx_export:
        planet_file = HDXExportRegion.objects.get(job_id=run.job_id).planet_file
    if is_partner_export:
        export_region = PartnerExportRegion.objects.get(job_id=run.job_id)
        planet_file = export_region.planet_file
        polygon_centroid = export_region.polygon_centroid

        # Run PDC special task.
        if export_region.group.name == "PDC" and planet_file is True and polygon_centroid is True:
            params = {
                "PLANET_FILE": settings.PLANET_FILE,
                "MAPPING": mapping,
                "STAGE_DIR": stage_dir,
                "DOWNLOAD_DIR": download_dir,
                "VALID_NAME": valid_name
            }

            if "geopackage" not in export_formats:
                raise ValueError("geopackage must be the export format")

            paths = run_pdc_task(params)

            start_task("geopackage")
            target = join(download_dir, "{}.gpkg".format(valid_name))
            shutil.move(paths["geopackage"], target)
            os.chmod(target, 0o644)

            finish_task("geopackage",[osm_export_tool.File("gpkg",[target],'')], planet_file)

            send_completion_notification(run)

            run.status = 'COMPLETED'
            run.finished_at = timezone.now()
            run.save()
            LOG.debug('Finished ExportRun with id: {0}'.format(run_uid))

            return

    if is_hdx_export:
        geopackage = None
        shp = None
        kml = None
        use_only_galaxy = False

        if galaxy_supported_outputs == list(export_formats) or set(export_formats).issubset(set(galaxy_supported_outputs)):
            use_only_galaxy=True

        tabular_outputs = []
        if 'geopackage' in export_formats:
            geopackage = tabular.MultiGeopackage(join(stage_dir,valid_name),mapping)
            tabular_outputs.append(geopackage)
            start_task('geopackage')

        if 'shp' in export_formats:
            shp = Galaxy(settings.GALAXY_API_URL,geom,mapping=mapping,file_name=f"{valid_name}_{run_uid}")
            # shp = tabular.Shapefile(join(stage_dir,valid_name),mapping)
            # tabular_outputs.append(shp)
            start_task('shp')

        if 'kml' in export_formats:
            kml = tabular.Kml(join(stage_dir,valid_name),mapping)
            tabular_outputs.append(kml)
            start_task('kml')

        if planet_file:
            h = tabular.Handler(tabular_outputs,mapping,polygon_centroid=polygon_centroid)
            source = OsmiumTool('osmium',settings.PLANET_FILE,geom,join(stage_dir,'extract.osm.pbf'),tempdir=stage_dir)
        
        else:
            mapping_filter = mapping
            if job.unfiltered:
                mapping_filter = None

            if use_only_galaxy == False : 
                h = tabular.Handler(tabular_outputs,mapping,clipping_geom=geom,polygon_centroid=polygon_centroid) 
                source = Overpass(settings.OVERPASS_API_URL,geom,join(stage_dir,'overpass.osm.pbf'),tempdir=stage_dir,use_curl=True,mapping=mapping_filter)

        if use_only_galaxy == False : 
            LOG.debug('Source start for run: {0}'.format(run_uid))
            source_path = source.path()
            LOG.debug('Source end for run: {0}'.format(run_uid))
            h.apply_file(source_path, locations=True, idx='sparse_file_array')

        all_zips = []

        def add_metadata(z,theme):
            columns = []
            for key in theme.keys:
                columns.append('{0} http://wiki.openstreetmap.org/wiki/Key:{0}'.format(key))
            columns = '\n'.join(columns)
            readme = ZIP_README.format(criteria=theme.matcher.to_sql(),columns=columns)
            z.writestr("README.txt", readme)

        if geopackage:
            geopackage.finalize()
            zips = []
            for theme in mapping.themes:
                destination = join(download_dir,valid_name + '_' + slugify(theme.name) + '_gpkg.zip')
                matching_files = [f for f in geopackage.files if 'theme' in f.extra and f.extra['theme'] == theme.name]
                with zipfile.ZipFile(destination, 'w', zipfile.ZIP_DEFLATED, True) as z:
                    add_metadata(z,theme)
                    for file in matching_files:
                        for part in file.parts:
                            z.write(part, os.path.basename(part))
                zips.append(osm_export_tool.File('geopackage',[destination],{'theme':theme.name}))
            finish_task('geopackage',zips)
            all_zips += zips

        if shp:
            response_back=shp.fetch('shp',is_hdx_export=True)
            finish_task('shp',response_back=response_back)
            # shp.finalize()
            # zips = []
            # for file in shp.files:
            #     # for HDX geopreview to work
            #     # each file (_polygons, _lines) is a separate zip resource
            #     # the zipfile must end with only .zip (not .shp.zip)
            #     destination = join(download_dir,os.path.basename(file.parts[0]).replace('.','_') + '.zip')
            #     with zipfile.ZipFile(destination, 'w', zipfile.ZIP_DEFLATED, True) as z:
            #         theme = [t for t in mapping.themes if t.name == file.extra['theme']][0]
            #         add_metadata(z,theme)
            #         for part in file.parts:
            #             z.write(part, os.path.basename(part))
            #     # print(file.extra['theme'])
            #     zips.append(osm_export_tool.File('shp',[destination],{'theme':file.extra['theme']}))
            # finish_task('shp',zips)
            # all_zips += zips

            all_zips += response_back

        if kml:
            kml.finalize()
            zips = []
            for file in kml.files:
                destination = join(download_dir,os.path.basename(file.parts[0]).replace('.','_') + '.zip')
                with zipfile.ZipFile(destination, 'w', zipfile.ZIP_DEFLATED, True) as z:
                    theme = [t for t in mapping.themes if t.name == file.extra['theme']][0]
                    add_metadata(z,theme)
                    for part in file.parts:
                        z.write(part, os.path.basename(part))
                zips.append(osm_export_tool.File('kml',[destination],{'theme':file.extra['theme']}))
            finish_task('kml',zips)
            all_zips += zips

        if 'garmin_img' in export_formats:
            start_task('garmin_img')
            garmin_files = nontabular.garmin(source_path,settings.GARMIN_SPLITTER,settings.GARMIN_MKGMAP,tempdir=stage_dir)
            zipped = create_package(join(download_dir,valid_name + '_gmapsupp_img.zip'),garmin_files,boundary_geom=geom,output_name='garmin_img')
            all_zips.append(zipped)
            finish_task('garmin_img',[zipped])

        if settings.SYNC_TO_HDX:
            print("Syncing to HDX")
            region = HDXExportRegion.objects.get(job_id=run.job_id)
            public_dir = settings.HOSTNAME + join(settings.EXPORT_MEDIA_ROOT, run_uid)
            sync_region(region,all_zips,public_dir)
        send_hdx_completion_notification(run, run.job.hdx_export_region_set.first())
    else:
        geopackage = None
        shp = None
        kml = None
        geojson=None
        tabular_outputs = []
        
        use_only_galaxy = False

        if galaxy_supported_outputs == list(export_formats) or set(export_formats).issubset(set(galaxy_supported_outputs)):
            use_only_galaxy=True

        if 'geopackage' in export_formats:
            geopackage = tabular.Geopackage(join(stage_dir,valid_name),mapping)
            tabular_outputs.append(geopackage)
            start_task('geopackage')

        if 'shp' in export_formats:
            shp = Galaxy(settings.GALAXY_API_URL,geom,mapping=mapping,file_name=f"{valid_name}_{run_uid}")
            # shp = tabular.Shapefile(join(stage_dir,valid_name),mapping)
            # tabular_outputs.append(shp)
            start_task('shp')
        
        if 'geojson' in export_formats:
            geojson = Galaxy(settings.GALAXY_API_URL,geom,mapping=mapping,file_name=valid_name)
            start_task('geojson')

        if 'kml' in export_formats:
            kml = tabular.Kml(join(stage_dir,valid_name),mapping)
            tabular_outputs.append(kml)
            start_task('kml')
        if planet_file:
            h = tabular.Handler(tabular_outputs,mapping,polygon_centroid=polygon_centroid)
            source = OsmiumTool('osmium',settings.PLANET_FILE,geom,join(stage_dir,'extract.osm.pbf'),tempdir=stage_dir, mapping=mapping)
        else:
            mapping_filter = mapping
            if job.unfiltered:
                mapping_filter = None 
            if use_only_galaxy == False : 
                h = tabular.Handler(tabular_outputs,mapping,clipping_geom=geom,polygon_centroid=polygon_centroid)
                source = Overpass(settings.OVERPASS_API_URL,geom,join(stage_dir,'overpass.osm.pbf'),tempdir=stage_dir,use_curl=True,mapping=mapping_filter)
        if use_only_galaxy == False :        
            LOG.debug('Source start for run: {0}'.format(run_uid))
            source_path = source.path()
            LOG.debug('Source end for run: {0}'.format(run_uid))

            h.apply_file(source_path, locations=True, idx='sparse_file_array')

        bundle_files = []
        

        if geopackage:
            geopackage.finalize()
            zipped = create_package(join(download_dir,valid_name + '_gpkg.zip'),geopackage.files,boundary_geom=geom)
            bundle_files += geopackage.files
            finish_task('geopackage',[zipped])

        if shp:
            # shp.finalize()
            # zipped = create_package(join(download_dir,valid_name + '_shp.zip'),shp.files,boundary_geom=geom)
            # bundle_files += shp.files
            # finish_task('shp',[zipped])
            response_back=shp.fetch('shp')
            finish_task('shp',response_back=response_back)

        if geojson :
            response_back=geojson.fetch('GeoJSON')
            finish_task('geojson',response_back=response_back)

        if kml:
            kml.finalize()
            zipped = create_package(join(download_dir,valid_name + '_kml.zip'),kml.files,boundary_geom=geom)
            bundle_files += kml.files
            finish_task('kml',[zipped])

        if 'garmin_img' in export_formats:
            start_task('garmin_img')
            garmin_files = nontabular.garmin(source_path,settings.GARMIN_SPLITTER,settings.GARMIN_MKGMAP,tempdir=stage_dir)
            bundle_files += garmin_files
            zipped = create_package(join(download_dir,valid_name + '_gmapsupp_img.zip'),garmin_files,boundary_geom=geom)
            finish_task('garmin_img',[zipped])

        if 'mwm' in export_formats:
            start_task('mwm')
            mwm_dir = join(stage_dir,'mwm')
            if not exists(mwm_dir):
                os.makedirs(mwm_dir)
            mwm_files = nontabular.mwm(source_path,mwm_dir,settings.GENERATE_MWM,settings.GENERATOR_TOOL)
            bundle_files += mwm_files
            zipped = create_package(join(download_dir,valid_name + '_mwm.zip'),mwm_files,boundary_geom=geom)
            finish_task('mwm',[zipped])

        if 'osmand_obf' in export_formats:
            start_task('osmand_obf')
            osmand_files = nontabular.osmand(source_path,settings.OSMAND_MAP_CREATOR_DIR,tempdir=stage_dir)
            bundle_files += osmand_files
            zipped = create_package(join(download_dir,valid_name + '_Osmand2_obf.zip'),osmand_files,boundary_geom=geom)
            finish_task('osmand_obf',[zipped])

        if 'mbtiles' in export_formats:
            start_task('mbtiles')
            mbtiles_files = nontabular.mbtiles(geom,join(stage_dir,valid_name + '.mbtiles'),job.mbtiles_source,job.mbtiles_minzoom,job.mbtiles_maxzoom)
            bundle_files += mbtiles_files
            zipped = create_package(join(download_dir,valid_name + '_mbtiles.zip'),mbtiles_files,boundary_geom=geom)
            finish_task('mbtiles',[zipped])

        if 'osm_pbf' in export_formats:
            bundle_files += [osm_export_tool.File('osm_pbf',[source_path],'')]

        if 'bundle' in export_formats:
            start_task('bundle')
            zipped = create_posm_bundle(join(download_dir,valid_name + '-bundle.tar.gz'),bundle_files,job.name,valid_name,job.description,geom)
            finish_task('bundle',[zipped])

        # do this last so we can do a mv instead of a copy
        if 'osm_pbf' in export_formats:
            start_task('osm_pbf')
            target = join(download_dir,valid_name + '.osm.pbf')
            shutil.move(source_path,target)
            os.chmod(target, 0o644)
            finish_task('osm_pbf',[osm_export_tool.File('pbf',[target],'')], planet_file)

        send_completion_notification(run)

    run.status = 'COMPLETED'
    run.finished_at = timezone.now()
    run.save()
    LOG.debug('Finished ExportRun with id: {0}'.format(run_uid))