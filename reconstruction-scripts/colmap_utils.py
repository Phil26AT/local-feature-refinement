# Adapted from https://github.com/ahojnnes/local-feature-evaluation/blob/master/scripts/reconstruction_pipeline.py.
# Copyright 2017, Johannes L. Schoenberger <jsch at inf.ethz.ch>.
import cv2

import multiprocessing

import numpy as np

import os

import pycolmap

import subprocess

import shutil

import sqlite3

import sys

from tqdm import tqdm

import types

import types_pb2

from refinement import refine_matches_coarse_to_fine

import sys

sys.path.append('..')
sys.path.append('../..')
import pickle 

from build import pyfmcolmap
from externals.hloc.hloc.utils.read_write_model import rotmat2qvec
from read_write_keypoints_h5 import read_keypointsh5


def generate_empty_reconstruction(reference_model_path, empty_model_path, holdout_image_names=[]):
    if not os.path.exists(empty_model_path):
        os.mkdir(empty_model_path)

    with open(os.path.join(reference_model_path, 'cameras.txt'), 'r') as f:
        raw_cameras = f.readlines()[3 :]

    with open(os.path.join(empty_model_path, 'cameras.txt'), 'w') as f:
        for raw_line in raw_cameras:
            f.write('%s\n' % raw_line)

    with open(os.path.join(reference_model_path, 'images.txt'), 'r') as f:
        raw_images = f.readlines()[4 :]

    images = {}
    for raw_line in raw_images[:: 2]:
        raw_line = raw_line.strip('\n').split(' ')
        image_path = raw_line[-1]
        image_name = image_path.split('/')[-1]
        image_id = int(raw_line[0])
        images[image_path] = image_id

    with open(os.path.join(empty_model_path, 'images.txt'), 'w') as f:
        for raw_line in raw_images[:: 2]:
            raw_line = raw_line.strip('\n')
            if raw_line.split(' ')[-1] in holdout_image_names:
                continue
            f.write('%s\n\n' % raw_line)

    with open(os.path.join(empty_model_path, 'points3D.txt'), 'w') as f:
        pass

    return images


def image_ids_to_pair_id(image_id1, image_id2):
    if image_id1 > image_id2:
        return 2147483647 * image_id2 + image_id1
    else:
        return 2147483647 * image_id1 + image_id2


def array_to_blob(array):
    return array.tostring()


def complete_keypoints(keypoints):
    if keypoints.shape[1] == 2:
        return np.hstack([
            keypoints, np.ones([keypoints.shape[0], 1]), np.zeros([keypoints.shape[0], 1])
        ])
    elif keypoints.shape[1] == 3:
        return np.hstack([
            keypoints, np.zeros([keypoints.shape[0], 1])
        ])
    else:
        return keypoints


def import_features(colmap_path, method_name, database_path, image_path, match_list_path, matches_file, solution_file, 
                holdout_image_names=[], stdout_file=None, h5keypoint_file=None):
    connection = sqlite3.connect(database_path)
    cursor = connection.cursor()

    cursor.execute(
        'SELECT name FROM sqlite_master WHERE type=\'table\' AND name=\'inlier_matches\';'
    )
    try:
        inlier_matches_table_exists = bool(next(cursor)[0])
    except StopIteration:
        inlier_matches_table_exists = False

    cursor.execute('DELETE FROM keypoints;')
    cursor.execute('DELETE FROM descriptors;')
    cursor.execute('DELETE FROM matches;')
    if inlier_matches_table_exists:
        cursor.execute('DELETE FROM inlier_matches;')
    else:
        cursor.execute('DELETE FROM two_view_geometries;')
    connection.commit()

    images = {}
    cursor.execute('SELECT name, image_id FROM images;')
    for row in cursor:
        images[row[0]] = row[1]

    # Import the features.
    if solution_file is not None:
        solution_file_proto = types_pb2.SolutionFile()
        with open(solution_file, 'rb') as f:
            solution_file_proto.ParseFromString(f.read())

        image_proto_idx = {}
        for idx, image in enumerate(solution_file_proto.images):
            image_proto_idx[image.image_name] = idx

    sum_num_features = 0
    for image_name, image_id in images.items():
        if image_name in holdout_image_names:
            continue
        
        if h5keypoint_file is not None:
            keypoints = read_keypointsh5(h5keypoint_file, image_name)
        else:
            keypoint_path = os.path.join(image_path, '%s.%s' % (image_name, method_name))
            features = np.load(keypoint_path, allow_pickle=True)
            keypoints = features['keypoints'][:, : 3]


        if keypoints.shape[0] == 0:
            keypoints = np.zeros([0, 4])
        keypoints = complete_keypoints(keypoints).astype(np.float32)

        num_features = keypoints.shape[0]
        sum_num_features += num_features

        if solution_file is not None:
            displacements = np.zeros([num_features, 2]).astype(np.float32)
            if image_name in image_proto_idx:
                for displacement in solution_file_proto.images[image_proto_idx[image_name]].displacements:
                    feature_idx = displacement.feature_idx
                    di = displacement.di
                    dj = displacement.dj
                    displacements[feature_idx, :] = [dj, di]
                fact = solution_file_proto.images[image_proto_idx[image_name]].fact
                displacements *= fact
            keypoints[:, : 2] += displacements * 16
        keypoints[:, : 2] += 0.5

        # descriptors = features['descriptors']

        assert keypoints.shape[1] == 4
        # assert keypoints.shape[0] == descriptors.shape[0]
        keypoints_str = array_to_blob(keypoints)
        cursor.execute(
            'INSERT INTO keypoints(image_id, rows, cols, data) VALUES(?, ?, ?, ?);',
            (image_id, keypoints.shape[0], keypoints.shape[1], keypoints_str)
        )
    connection.commit()

    matches_files = []
    if os.path.exists(matches_file):
        matches_files.append(matches_file)
    else:
        part_idx = 0
        while os.path.exists('%s.part.%d' % (matches_file, part_idx)):
            matches_files.append('%s.part.%d' % (matches_file, part_idx))
            part_idx += 1

    image_pairs = []
    image_pair_ids = set()

    for matches_file in matches_files:
        matching_file_proto = types_pb2.MatchingFile()
        with open(matches_file, 'rb') as f:
            matching_file_proto.ParseFromString(f.read())

        for image_pair in matching_file_proto.image_pairs:
            image_name1, image_name2 = image_pair.image_name1, image_pair.image_name2
            if image_name1 in holdout_image_names or image_name2 in holdout_image_names:
                continue

            image_pairs.append((image_name1, image_name2))
            image_id1, image_id2 = images[image_name1], images[image_name2]
            image_pair_id = image_ids_to_pair_id(image_id1, image_id2)
            if image_pair_id in image_pair_ids:
                continue
            image_pair_ids.add(image_pair_id)

            matches = []
            for match in image_pair.matches:
                matches.append([int(match.feature_idx1), int(match.feature_idx2)])

            matches = np.array(matches).astype(np.uint32)
            if matches.shape[0] == 0:
                matches = np.zeros([0, 2])
            assert matches.shape[1] == 2
            if image_id1 > image_id2:
                matches = matches[:, [1, 0]]
            matches_str = array_to_blob(matches)
            cursor.execute(
                'INSERT INTO matches(pair_id, rows, cols, data) VALUES(?, ?, ?, ?);',
                (image_pair_id, matches.shape[0], matches.shape[1], matches_str)
            )
        connection.commit()

    cursor.close()
    connection.close()

    # Run geometric verification.
    if stdout_file is None:
        stdout = sys.__stdout__
    else:
        stdout = open(stdout_file, 'a')

    subprocess.call([
        os.path.join(colmap_path, 'colmap'), 'matches_importer',
        '--database_path', database_path,
        '--match_list_path', match_list_path,
        '--match_type', 'pairs'
    ], stdout=stdout)

    # Recover statistics.
    connection = sqlite3.connect(database_path)
    cursor = connection.cursor()

    cursor.execute('SELECT count(*) FROM images;')
    num_images = next(cursor)[0]

    cursor.execute('SELECT count(*) FROM two_view_geometries WHERE rows > 0;')
    num_inlier_pairs = next(cursor)[0]

    cursor.execute('SELECT sum(rows) FROM two_view_geometries WHERE rows > 0;')
    num_inlier_matches = next(cursor)[0]

    cursor.close()
    connection.close()

    return dict(
        num_images=num_images,
        num_inlier_pairs=num_inlier_pairs,
        num_inlier_matches=num_inlier_matches,
        avg_num_features=(sum_num_features / num_images)
    )


def reconstruct(colmap_path, database_path, image_path, sparse_path):
    # Run the sparse reconstruction.
    if not os.path.exists(sparse_path):
        os.mkdir(sparse_path)
    subprocess.call([
        os.path.join(colmap_path, 'colmap'), 'mapper',
        '--database_path', database_path,
        '--image_path', image_path,
        '--output_path', sparse_path,
        '--Mapper.num_threads', str(min(multiprocessing.cpu_count(), 8))
    ])

    # Find the largest reconstructed sparse model.
    models = os.listdir(sparse_path)
    if len(models) == 0:
        print('Warning: Could not reconstruct any model')
        return

    largest_model = None
    largest_model_num_images = 0
    for model in models:
        subprocess.call([
            os.path.join(colmap_path, 'colmap'), 'model_converter',
            '--input_path', os.path.join(sparse_path, model),
            '--output_path', os.path.join(sparse_path, model),
            '--output_type', 'TXT'
        ])
        with open(os.path.join(sparse_path, model, 'cameras.txt'), 'r') as fid:
            for line in fid:
                if line.startswith('# Number of cameras'):
                    num_images = int(line.split()[-1])
                    if num_images > largest_model_num_images:
                        largest_model = model
                        largest_model_num_images = num_images
                    break

    assert(largest_model_num_images > 0)

    largest_model_path = os.path.join(sparse_path, largest_model)

    # Recover model statistics.
    stats = subprocess.check_output([
        os.path.join(colmap_path, 'colmap'), 'model_analyzer',
        '--path', largest_model_path
    ])

    stats = stats.decode().split('\n')
    for stat in stats:
        if stat.startswith('Registered images'):
            num_reg_images = int(stat.split()[-1])
        elif stat.startswith('Points'):
            num_sparse_points = int(stat.split()[-1])
        elif stat.startswith('Observations'):
            num_observations = int(stat.split()[-1])
        elif stat.startswith('Mean track length'):
            mean_track_length = float(stat.split()[-1])
        elif stat.startswith('Mean observations per image'):
            num_observations_per_image = float(stat.split()[-1])
        elif stat.startswith('Mean reprojection error'):
            mean_reproj_error = float(stat.split()[-1][:-2])

    return dict(
        num_reg_images=num_reg_images,
        num_sparse_points=num_sparse_points,
        num_observations=num_observations,
        mean_track_length=mean_track_length,
        num_observations_per_image=num_observations_per_image,
        mean_reproj_error=mean_reproj_error
    )


def triangulate(colmap_path, database_path, image_path, empty_model_path, model_path, ply_model_path, stdout_file=None):
    if stdout_file is None:
        stdout = sys.__stdout__
    else:
        stdout = open(stdout_file, 'a')

    # Triangulate the database model.
    if not os.path.isdir(model_path):
        os.mkdir(model_path)

    subprocess.call([
        os.path.join(colmap_path, 'colmap'), 'point_triangulator',
        '--database_path', database_path,
        '--image_path', image_path,
        '--input_path', empty_model_path,
        '--output_path', model_path,
        '--Mapper.ba_refine_focal_length', '0',
        '--Mapper.ba_refine_principal_point', '0',
        '--Mapper.ba_refine_extra_params', '0'
    ], stdout=stdout)

    # Convert model to TXT.
    subprocess.call([
        os.path.join(colmap_path, 'colmap'), 'model_converter',
        '--input_path', model_path,
        '--output_path', model_path,
        '--output_type', 'TXT'
    ], stdout=stdout)

    # Convert model to PLY.
    if ply_model_path is not None:
        subprocess.call([
            os.path.join(colmap_path, 'colmap'), 'model_converter',
            '--input_path', model_path,
            '--output_path', ply_model_path,
            '--output_type', 'PLY'
        ], stdout=stdout)
    print(ply_model_path, model_path)
    # Model stats.
    # subprocess.call([
    #     os.path.join(colmap_path, 'colmap'), 'model_analyzer',
    #     '--path', model_path
    # ], stdout=stdout)


def parse_raw_pose(raw_pose):
    qw, qx, qy, qz, tx, ty, tz = map(float, raw_pose)
    qvec = np.array([qw, qx, qy, qz])
    qvec = qvec / np.linalg.norm(qvec)
    R = qvec_to_rotmat(qvec)
    t = np.array([tx, ty, tz])
    pose = np.zeros([3, 4])
    pose[: 3, : 3] = R
    pose[: 3, 3] = t
    return pose


def colmap_pose_to_matrix(qvec, tvec):
    qvec = qvec / np.linalg.norm(qvec)
    R = qvec_to_rotmat(qvec)
    pose = np.zeros([3, 4])
    pose[: 3, : 3] = R
    pose[: 3, 3] = tvec
    return pose


def qvec_to_rotmat(qvec):
    w, x, y, z = qvec
    R = np.array([
        [
            1 - 2 * y * y - 2 * z * z,
            2 * x * y - 2 * z * w,
            2 * x * z + 2 * y * w
        ],
        [
            2 * x * y + 2 * z * w,
            1 - 2 * x * x - 2 * z * z,
            2 * y * z - 2 * x * w
        ],
        [
            2 * x * z - 2 * y * w,
            2 * y * z + 2 * x * w,
            1 - 2 * x * x - 2 * y * y
        ]
    ])
    return R


def world_to_image(u, v, K):
    x = K[0, 0] * u + K[0, 2]
    y = K[1, 1] * v + K[1, 2]

    return x, y


def compute_reconstruction_statistics(reference_model_path):
    # Images w. intrinsics and extrinsics.
    with open(os.path.join(reference_model_path, 'cameras.txt'), 'r') as f:
        raw_cameras = f.readlines()[3 :]

    cameras = {}
    for raw_line in raw_cameras:
        split_line = raw_line.strip('\n').split(' ')
        cameras[int(split_line[0])] = split_line[1 :]

    with open(os.path.join(reference_model_path, 'images.txt'), 'r') as f:
        raw_images = f.readlines()[4 :]

    images = {}
    poses = {}
    intrinsics = {}
    for raw_line in raw_images[:: 2]:
        raw_line = raw_line.strip('\n').split(' ')
        image_path = raw_line[-1]
        image_name = image_path.split('/')[-1]
        image_id = int(raw_line[0])
        camera_id = int(raw_line[-2])
        intrinsics[image_path] = cameras[camera_id]
        images[image_path] = image_id
        poses[image_path] = parse_raw_pose(raw_line[1 : -2])

    # Covisibility matrix.
    image_visible_points3D = {}
    max_image_id = 0
    with open(os.path.join(reference_model_path, 'images.txt')) as images_file:
        lines = images_file.readlines()
        lines = lines[4 :]  # Skip the header.
        raw_poses = [line.strip('\n').split(' ') for line in lines[:: 2]]
        raw_points = [line.strip('\n').split(' ') for line in lines[1 :: 2]]
        for raw_pose, raw_pts in zip(raw_poses, raw_points):
            # image_id, qw, qx, qy, qz, tx, ty, tz, camera_id, name
            # points2D[(x, y, point3D_id)]
            image_id = int(raw_pose[0])
            max_image_id = max(max_image_id, image_id)
            point3D_ids = map(int, raw_pts[2 :: 3])
            image_visible_points3D[image_id] = set()
            for point3D_id in point3D_ids:
                if point3D_id == -1:
                    continue
                image_visible_points3D[image_id].add(point3D_id)

    n_covisible_points = np.zeros([max_image_id + 1, max_image_id + 1])
    # Fill upper triangle.
    for image_id1 in image_visible_points3D.keys():
        for image_id2 in image_visible_points3D.keys():
            if image_id1 > image_id2:
                continue
            visible_points3D1 = image_visible_points3D[image_id1]
            visible_points3D2 = image_visible_points3D[image_id2]
            n_covisible_points[image_id1, image_id2] = len(visible_points3D1 & visible_points3D2)
            # Mirror to lower triangle.
            n_covisible_points[image_id2, image_id1] = n_covisible_points[image_id1, image_id2]

    return images, intrinsics, poses, n_covisible_points


def parse_reconstruction(scene_path):
    cameras = {}
    with open(os.path.join(scene_path, 'cameras.txt')) as cameras_file:
        lines = cameras_file.readlines()
        lines = lines[3 :]  # Skip the header.
        raw_cameras = [line.strip('\n').split(' ') for line in lines]
        for raw_camera in raw_cameras:
            # camera_id, model, width, height, params[]
            camera_id = int(raw_camera[0])
            assert(raw_camera[1] == 'PINHOLE')
            _, _, fx, fy, cx, cy = map(float, raw_camera[2 :])
            K = np.array([
                [fx, 0, cx],
                [0, fy, cy],
                [0, 0, 1]
            ])
            cameras[camera_id] = K

    points3D = {}
    with open(os.path.join(scene_path, 'points3D.txt')) as images_file:
        lines = images_file.readlines()
        lines = lines[3 :]  # Skip the header.
        raw_points = [line.strip('\n').split(' ') for line in lines]
        for raw_point in raw_points:
            # point3D_id, x, y, z, r, g, b, error, track[(image_id, point2D_idx)]
            point_id = int(raw_point[0])
            x, y, z = map(float, raw_point[1 : 4])
            points3D[point_id] = np.array([x, y, z])

    points2D_idx_to_points3D_id = {}
    points2D_idx_reprojected = {}
    with open(os.path.join(scene_path, 'images.txt')) as images_file:
        lines = images_file.readlines()
        lines = lines[4 :]  # Skip the header.
        raw_poses = [line.strip('\n').split(' ') for line in lines[:: 2]]
        raw_points = [line.strip('\n').split(' ') for line in lines[1 :: 2]]
        for raw_pose, raw_pts in zip(raw_poses, raw_points):
            # image_id, qw, qx, qy, qz, tx, ty, tz, camera_id, name
            # points2D[(x, y, point3D_id)]
            image_id = int(raw_pose[0])
            camera_id = int(raw_pose[-2])
            image_name = raw_pose[-1]
            K = cameras[camera_id]
            P = parse_raw_pose(raw_pose[1 : -2])
            # xs = map(float, raw_pts[0 :: 3])
            # ys = map(float, raw_pts[1 :: 3])
            point3D_ids = map(int, raw_pts[2 :: 3])
            c_points2D_idx_to_points3D_id = {}
            c_points2D_idx_reprojected = {}

            for point2D_idx, point3D_id in enumerate(point3D_ids):
                if point3D_id == -1:
                    continue

                c_points2D_idx_to_points3D_id[point2D_idx] = point3D_id

                # Reproject point.
                point3D = points3D[point3D_id]
                camera_point3D = P[: 3, : 3] @ point3D + P[: 3, 3]
                u = camera_point3D[0] / camera_point3D[2]
                v = camera_point3D[1] / camera_point3D[2]
                point2D = world_to_image(u, v, K)
                c_points2D_idx_reprojected[point2D_idx] = point2D

            points2D_idx_to_points3D_id[image_name] = c_points2D_idx_to_points3D_id
            points2D_idx_reprojected[image_name] = c_points2D_idx_reprojected

    return points3D, points2D_idx_to_points3D_id, points2D_idx_reprojected


def localize(
        image_id, image_name, camera_dict, holdout_image_names, numpy_images, facts, net, device, batch_size,
        colmap_path, dataset_name, dataset_path, method_name, refine, matching_file_proto,
        dummy_database_path, image_path, reference_model_path, match_list_path, levels
):
    # Define local paths.
    print("Localize")
    partial_paths = types.SimpleNamespace()
    partial_paths.log_file = os.path.join(
        'output',
        '%s-%s-%s.loc-log.txt' % (method_name, dataset_name, 'ref' if refine else 'raw')
    )

    partial_root = os.path.join(
        dataset_path, 'loc', f'partial-{image_id}-{"ref" if refine else "raw"}')

    partial_paths.database_path = os.path.join(
        # dataset_path, method_name + '-partial.db'
        partial_root, method_name + '-partial.db'
    )
    partial_paths.model_path = os.path.join(
        # dataset_path, 'sparse-%s-partial' % method_name
        partial_root, 'sparse-%s-partial' % method_name
    )

    dataset_path_local = os.path.join('/cluster/scratch/plindenbe/ETH3D/', dataset_name)

    partial_paths.fm_model_path = os.path.join(
        # dataset_path, 'sparse-%s-partial' % method_name
        dataset_path_local, 'sparse-%s-partial-fmref-loc' % method_name
    )

    partial_paths.empty_model_path = os.path.join(
        # dataset_path, 'sparse-%s-partial-empty' % method_name
        partial_root, 'sparse-%s-partial-empty' % method_name
    )
    if refine:
        partial_paths.solution_file = os.path.join(
            # dataset_path, '%s-partial-solution.pb' % method_name
            partial_root, '%s-partial-solution.pb' % method_name
        )
    else:
        partial_paths.solution_file = None
    partial_paths.match_list_file = os.path.join(
        # dataset_path, 'match_list_partial_%s.txt' % method_name
        partial_root, 'match_list_partial_%s.txt' % method_name
    )
    # Start logging.
    with open(partial_paths.log_file, 'a') as f:
        f.write('%d %s\n' % (image_id, image_name))

    # Recover matches.
    # print("Before loading matches", matches_file)
    # matching_file_proto = types_pb2.MatchingFile()
    # with open(matches_file, 'rb') as f:
    #     matching_file_proto.ParseFromString(f.read())
    all_matches = {}
    all_matching_scores = {}
    for image_pair in matching_file_proto.image_pairs:
        if image_pair.image_name1 == image_name:
            matches = []
            scores = []
            for match in image_pair.matches:
                matches.append([int(match.feature_idx1), int(match.feature_idx2)])
                scores.append(match.similarity)
            all_matches[image_pair.image_name2] = np.array(matches).astype(np.uint32)
            all_matching_scores[image_pair.image_name2] = np.array(scores)
        elif image_pair.image_name2 == image_name:
            matches = []
            scores = []
            for match in image_pair.matches:
                matches.append([int(match.feature_idx2), int(match.feature_idx1)])
                scores.append(match.similarity)
            all_matches[image_pair.image_name1] = np.array(matches).astype(np.uint32)
            all_matching_scores[image_pair.image_name1] = np.array(scores)
    # Empty reconstruction.
    # _ = generate_empty_reconstruction(reference_model_path, partial_paths.empty_model_path, holdout_image_names=holdout_image_names)

    # Re-run solve-DFS.
    # if refine:
    #     subprocess.call([
    #         'multi-view-refinement/build/solve',
    #         '--matches_file=%s' % matches_file,
    #         '--output_file=%s' % partial_paths.solution_file
    #     ] + [
    #         ('--holdout_images=%s' % image_name) for image_name in holdout_image_names
    #     ], stdout=open(partial_paths.log_file, 'a'))

    # # Create a new database.
    # shutil.copyfile(dummy_database_path, partial_paths.database_path)
    
    # # Prepare GV list.
    # with open(match_list_path, 'r') as f:
    #     lines = f.readlines()
    # with open(partial_paths.match_list_file, 'w') as f:
    #     for line in lines:
    #         line_aux = line.strip('\n').split(' ')
    #         if line_aux[0] in holdout_image_names or line_aux[1] in holdout_image_names:
    #             continue
    #         f.write(line)
    
    # Import features to a new database.
    # import_features(
    #     colmap_path, method_name, partial_paths.database_path, image_path,
    #     partial_paths.match_list_file, matches_file, partial_paths.solution_file,
    #     holdout_image_names=holdout_image_names,
    #     stdout_file=partial_paths.log_file
    # )

    # Triangulate model.
    # triangulate(colmap_path, partial_paths.database_path, image_path, partial_paths.empty_model_path, partial_paths.model_path, None, stdout_file=partial_paths.log_file)

    # Refine Model
    """
    Our triangulation script
    """
    
    fmcolmap_dict = {
        "s2d_mean" : ["--Featuremap.type","python128",
            "--Featuremap.dtype","half",
            "--Featuremap.python_levels",str(levels),
            "--FeatureBundleAdjustment.optimization_mode", "0",
            "--Featuremap.interpolation_mode", "1",
            '--Featuremap.python_command','python -m features.s2dnet',
            '--FeatureBundleAdjustment.use_embedded_point_iterations', "1",
            "--FeatureBundleAdjustment.feature_loss_function","geman",
            "--FeatureBundleAdjustment.feature_loss_function_scale", "0.1",
            "--Featuremap.batch_size", "100",
            "--Featuremap.sparse", "0",
            "--Featuremap.extract_squared_distance_maps", "0",
            "--Featuremap.sparse_patch_size", "10",
            "--FeatureBundleAdjustment.point_optimization_enabled", "0",
            "--FeatureBundleAdjustment.feature_regularization_enabled", "0",
            "--FeatureBundleAdjustment.feature_regularization_track_weighting", "0",
            "--FeatureBundleAdjustment.point_loss_function","geman",
            "--FeatureBundleAdjustment.point_loss_function_scale","0.3",
            "--FeatureBundleAdjustment.point_loss_function_magnitude","1.0",
            "--FeatureBundleAdjustment.berhu_slope","1.0",
            "--FeatureBundleAdjustment.max_num_iterations","2",
            "--patch_size", "1"],
    }
    fm_triangulation = True
    if fm_triangulation:
        print("Feature Triangulation: ", partial_paths.model_path, partial_paths.fm_model_path)
        #fmpath = partial_paths.model_path + "/fmreconstruction/"
        cmd = ["/cluster/home/plindenbe/FeatureMetricSfM/build/fmcolmap", "feature_bundle_adjuster", 
            '--image_path', image_path,
            '--input_path', partial_paths.model_path,
            '--output_path', partial_paths.fm_model_path,
            '--FeatureBundleAdjustment.refine_focal_length', '0',
            '--FeatureBundleAdjustment.refine_principal_point', '0',
            '--FeatureBundleAdjustment.refine_extra_params', '0',
            '--FeatureBundleAdjustment.refine_extrinsics', '0',
            '--Featuremap.load_from_cache', "1",
            '--Featuremap.cache_path', os.getenv("TMPDIR") + "/cache/" + dataset_name] + fmcolmap_dict["s2d_mean"]
        print(' '.join(cmd))
        subprocess.call(cmd)
        
        # Convert model to TXT.
        subprocess.call([
            os.path.join(colmap_path, 'colmap'), 'model_converter',
            '--input_path', partial_paths.fm_model_path,
            '--output_path', partial_paths.fm_model_path,
            '--output_type', 'TXT'
        ])
    # Parse reconstruction.
    print(partial_paths.model_path)
    points3D, points2D_idx_to_points3D_id, points2D_idx_reprojected = parse_reconstruction(partial_paths.fm_model_path if fm_triangulation else partial_paths.model_path)

    # 2D-3D matching.
    image1 = numpy_images[image_name]
    fact1 = facts[image_name]
    print(os.path.join(
        image_path, '%s.%s' % (image_name, method_name)))
    # Load the features.
    features1 = np.load(os.path.join(
        image_path, '%s.%s' % (image_name, method_name)
    ), allow_pickle=True)
    keypoints1 = features1['keypoints'][:, : 2]
    descriptors1 = features1['descriptors']
    print("1")
    if keypoints1.shape[0] != 0:
        matched_tracks = [{} for _ in range(keypoints1.shape[0])]
        matched_tracks_scores = [{} for _ in range(keypoints1.shape[0])]
        for image_name2, matches in all_matches.items():
            if image_name2 in holdout_image_names:
                continue
            image2 = numpy_images[image_name2]
            fact2 = facts[image_name2]
            
            scores = all_matching_scores[image_name2]
            p2D_idx_reprojected = points2D_idx_reprojected[image_name2]
            p2D_idx_to_p3D_id = points2D_idx_to_points3D_id[image_name2]

            valid = np.zeros(matches.shape[0], dtype=bool)
            valid_keypoints1 = []
            reprojected_keypoints2 = []
            for match_idx in range(matches.shape[0]):
                if matches[match_idx, 1] in p2D_idx_reprojected:
                    valid[match_idx] = True
                    reprojected_keypoints2.append(p2D_idx_reprojected[matches[match_idx, 1]])
                    valid_keypoints1.append(keypoints1[matches[match_idx, 0]])
            valid_keypoints1 = np.array(valid_keypoints1)
            reprojected_keypoints2 = np.array(reprojected_keypoints2)

            matches = matches[valid]
            scores = scores[valid]

            if np.sum(valid) == 0:
                continue

            # Keypoint refinement.
            if refine:
                displacements = refine_matches_coarse_to_fine(
                    image2, (reprojected_keypoints2 - .5) * 1 / fact2,
                    image1, valid_keypoints1 * 1 / fact1,
                    np.array([[i, i] for i in range(reprojected_keypoints2.shape[0])]),
                    net, device, batch_size, symmetric=False, grid=False
                )
                displacements *= fact1
            else:
                displacements = np.zeros([matches.shape[0], 2])
            displacements = displacements[:, [1, 0]]

            for match_idx in range(matches.shape[0]):
                p2D_idx1, p2D_idx2 = matches[match_idx]
                p3D_id = p2D_idx_to_p3D_id[p2D_idx2]
                if p3D_id not in matched_tracks[p2D_idx1]:
                    matched_tracks[p2D_idx1][p3D_id] = []
                    matched_tracks_scores[p2D_idx1][p3D_id] = []
                matched_tracks[p2D_idx1][p3D_id].append(keypoints1[p2D_idx1, : 2] + displacements[match_idx, :] * 16.)
                matched_tracks_scores[p2D_idx1][p3D_id].append(scores[match_idx])

        # PnP.
        pnp_points2D = []
        pnp_points3D = []
        pnp_points3D_id = []
        for p2D_idx, tracks in enumerate(matched_tracks):
            for p3D_id in tracks:
                p3D = points3D[p3D_id]
                p2D = .5 + np.average(
                    tracks[p3D_id], axis=0,
                    weights=matched_tracks_scores[p2D_idx][p3D_id]
                )

                pnp_points3D.append(p3D)
                pnp_points2D.append(p2D)
                pnp_points3D_id.append(p3D_id)

        pose_dict = pycolmap.absolute_pose_estimation(pnp_points2D, pnp_points3D, camera_dict, 12)
        tvec = pose_dict["tvec"]
        qvec = pose_dict["qvec"]

        if pose_dict['success']:
            refined_pose = colmap_pose_to_matrix(pose_dict['qvec'], pose_dict['tvec'])
        else:
            pose = None
            refined_pose = None
            refined_loc_pose = None

        # Load pose from PnP
        # T = np.loadtxt(os.path.join(partial_root, f'pnp-pose-{method_name}.txt'))
        # tvec, qvec = T[:3, 3], rotmat2qvec(T[:3, :3])

        cmd = ['--FeatureBundleAdjustment.refine_focal_length', '0',
            '--FeatureBundleAdjustment.refine_principal_point', '0',
            '--FeatureBundleAdjustment.refine_extra_params', '0',
            '--FeatureBundleAdjustment.refine_extrinsics', '1',
            "--Featuremap.type","python128",
            "--Featuremap.dtype","half",
            "--Featuremap.python_levels",str(levels),
            "--FeatureBundleAdjustment.optimization_mode", "0",
            "--Featuremap.interpolation_mode", "1",
            "--FeatureBundleAdjustment.feature_loss_function","cauchy",
            "--FeatureBundleAdjustment.feature_loss_function_scale", "1.0",
            "--Featuremap.batch_size", "100",
            "--Featuremap.sparse", "0",
            "--Featuremap.extract_squared_distance_maps", "0",
            "--Featuremap.sparse_patch_size", "10",
            "--FeatureBundleAdjustment.point_optimization_enabled", "0",
            "--FeatureBundleAdjustment.feature_regularization_enabled", "0",
            "--FeatureBundleAdjustment.feature_regularization_track_weighting", "0",
            "--FeatureBundleAdjustment.point_loss_function","tolerant",
            "--FeatureBundleAdjustment.point_loss_function_scale","1.0",
            "--FeatureBundleAdjustment.point_loss_function_magnitude","0.01",
            "--FeatureBundleAdjustment.berhu_slope","1.0",
            "--FeatureBundleAdjustment.max_num_iterations","10",
            "--patch_size", "1",
            '--Featuremap.load_from_cache', "1",
            '--Featuremap.cache_path', os.getenv("TMPDIR") + "/cache/" + dataset_name]

        dump_path = os.path.join(
                    partial_root, f'pnp-dict-{method_name}-{"raw"}.pkl')
        with open(dump_path, 'rb') as f:
            saved_pose_dict = pickle.load(f)

        # print(cmd)
        pose = colmap_pose_to_matrix(saved_pose_dict['qvec'], saved_pose_dict['tvec'])

        if fm_triangulation:
            saved_pose_dict = pose_dict
        refined_pose_dict = pyfmcolmap.feature_pose_refinement(
            np.array(pnp_points2D)[saved_pose_dict["inliers"]],
            np.array(pnp_points3D_id)[saved_pose_dict["inliers"]],
            saved_pose_dict['qvec'],
            saved_pose_dict['tvec'],
            image_name,
            camera_dict,
            partial_paths.model_path,
            image_path,
            " ".join(cmd))
        #print(refined_pose_dict["pose_change"])
        refined_loc_pose = colmap_pose_to_matrix(refined_pose_dict['qvec'], refined_pose_dict['tvec'])
       
    else:
        pose = None
        refined_pose = None
        refined_loc_pose = None

    # Remove auxiliary files.
    # os.remove(partial_paths.database_path)
    # if refine:
    #     os.remove(partial_paths.solution_file)
    # os.remove(partial_paths.match_list_file)

    # shutil.rmtree(partial_paths.empty_model_path)
    # shutil.rmtree(partial_paths.model_path)

    return pose, refined_pose, refined_loc_pose
