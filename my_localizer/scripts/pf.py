#!/usr/bin/env python

""" This is the starter code for the robot localization project """

import rospy

from dynamic_reconfigure.server import Server
from my_localizer.cfg import PfConfig

from std_msgs.msg import Header, String, ColorRGBA
from sensor_msgs.msg import LaserScan
from geometry_msgs.msg import PoseStamped, PoseWithCovarianceStamped, PoseArray, Pose, Point, Quaternion, Vector3
from visualization_msgs.msg import Marker, MarkerArray
from nav_msgs.srv import GetMap
from copy import deepcopy

import tf
from tf import TransformListener
from tf import TransformBroadcaster
from tf.transformations import euler_from_quaternion, rotation_matrix, quaternion_from_matrix
from random import gauss

import math
import time

import numpy as np
import matplotlib.pyplot as plt
import matplotlib.colors as colors
import matplotlib.cm as cmx 
from numpy.random import random_sample
from sklearn.neighbors import NearestNeighbors
from occupancy_field import OccupancyField
from scipy.stats import norm

from helper_functions import (convert_pose_inverse_transform,
                              convert_translation_rotation_to_pose,
                              convert_pose_to_xy_and_theta,
                              angle_diff)


class Particle(object):
    """ Represents a hypothesis (particle) of the robot's pose consisting of x,y and theta (yaw)
        Attributes:
            x: the x-coordinate of the hypothesis relative to the map frame
            y: the y-coordinate of the hypothesis relative ot the map frame
            theta: the yaw of the hypothesis relative to the map frame
            w: the particle weight (the class does not ensure that particle weights are normalized
    """

    def __init__(self,x=0.0,y=0.0,theta=0.0,w=1.0):
        """ Construct a new Particle
            x: the x-coordinate of the hypothesis relative to the map frame
            y: the y-coordinate of the hypothesis relative ot the map frame
            theta: the yaw of the hypothesis relative to the map frame
            w: the particle weight (the class does not ensure that particle weights are normalized """ 
        self.w = w
        self.theta = theta
        self.x = x
        self.y = y

    def as_pose(self):
        """ A helper function to convert a particle to a geometry_msgs/Pose message """
        orientation_tuple = tf.transformations.quaternion_from_euler(0,0,self.theta)
        return Pose(position=Point(x=self.x,y=self.y,z=0), orientation=Quaternion(x=orientation_tuple[0], y=orientation_tuple[1], z=orientation_tuple[2], w=orientation_tuple[3]))

    def normalize_weight(self, total_weight):
        """adjust the particle weight using the normalization factor"""
        self.w /= total_weight

class ParticleFilter:
    """ The class that represents a Particle Filter ROS Node
        Attributes list:
            initialized: a Boolean flag to communicate to other class methods that initializaiton is complete
            base_frame: the name of the robot base coordinate frame (should be "base_link" for most robots)
            map_frame: the name of the map coordinate frame (should be "map" in most cases)
            odom_frame: the name of the odometry coordinate frame (should be "odom" in most cases)
            scan_topic: the name of the scan topic to listen to (should be "scan" in most cases)
            n_particles: the number of particles in the filter
            d_thresh: the amount of linear movement before triggering a filter update
            a_thresh: the amount of angular movement before triggering a filter update
            laser_max_distance: the maximum distance to an obstacle we should use in a likelihood calculation
            pose_listener: a subscriber that listens for new approximate pose estimates (i.e. generated through the rviz GUI)
            particle_pub: a publisher for the particle cloud
            laser_subscriber: listens for new scan data on topic self.scan_topic
            tf_listener: listener for coordinate transforms
            tf_broadcaster: broadcaster for coordinate transforms
            particle_cloud: a list of particles representing a probability distribution over robot poses
            current_odom_xy_theta: the pose of the robot in the odometry frame when the last filter update was performed.
                                   The pose is expressed as a list [x,y,theta] (where theta is the yaw)
            map: the map we will be localizing ourselves in.  The map should be of type nav_msgs/OccupancyGrid
    """
    def __init__(self):
        self.initialized = False        # make sure we don't perform updates before everything is setup
        rospy.init_node('pf')           # tell roscore that we are creating a new node named "pf"

        self.base_frame = "base_link"   # the frame of the robot base
        self.map_frame = "map"          # the name of the map coordinate frame
        self.odom_frame = "odom"        # the name of the odometry coordinate frame
        self.scan_topic = "scan"        # the topic where we will get laser scans from 

        self.sample_factor = rospy.get_param('~sample_factor', 0.25)
        self.n_particles = int(self.sample_factor * rospy.get_param('~n_particles', 300))/self.sample_factor          # the number of particles to use

        self.d_thresh = 0.2             # the amount of linear movement before performing an update
        self.a_thresh = math.pi/6       # the amount of angular movement before performing an update

        self.laser_max_distance = 2.0   # maximum penalty to assess in the likelihood field model

        # dynamically configured parameters
        \
        self.model_noise_rate = rospy.get_param('~model_noise_rate', 0.05)
        self.model_noise_floor = rospy.get_param('~model_noise_floor', 0.05)

        self.linear_initialization_sigma = rospy.get_param('~linear_initialization_sigma', 0.2)
        self.angular_initialization_sigma = rospy.get_param('~angular_initialization_sigma', 5.0)

        self.linear_resample_sigma = rospy.get_param('~linear_resample_sigma', 0.1)
        self.angular_resample_sigma = rospy.get_param('~angular_resample_sigma', 5)*math.pi/180

        # Setup pubs and subs

        # pose_listener responds to selection of a new approximate robot location (for instance using rviz)
        self.pose_listener = rospy.Subscriber("initialpose", PoseWithCovarianceStamped, self.update_initial_pose)
        # publish the current particle cloud.  This enables viewing particles in rviz.
        self.particle_pub = rospy.Publisher("particlecloud", PoseArray, queue_size=10)
        self.particle_color_pub = rospy.Publisher("color_particlecloud", MarkerArray, queue_size=10)

        # laser_subscriber listens for data from the lidar
        self.laser_subscriber = rospy.Subscriber(self.scan_topic, LaserScan, self.scan_received)

        # setup the GetMap service
        getmap = rospy.ServiceProxy("static_map", GetMap)

        # enable listening for and broadcasting coordinate transforms
        self.tf_listener = TransformListener()
        self.tf_broadcaster = TransformBroadcaster()

        self.particle_cloud = []

        self.current_odom_xy_theta = []

        self.normal_dist = norm(0, self.model_noise_rate)

        # setup the dynamic reconfigure server
        srv = Server(PfConfig, self.config_callback)

        #self.fig = plt.figure()
        #self.fig.show()

        # request the map from the map server, the map should be of type nav_msgs/OccupancyGrid
        try:
            got_map = getmap()
            print "Got the map!"
        except rospy.ServiceException as exc:
            print("Service did not proess request: " + str(exc))

        # for now we have commented out the occupancy field initialization until you can successfully fetch the map
        self.occupancy_field = OccupancyField(got_map.map)
        self.initialized = True
        print "Initialization complete!"

    def config_callback(self, config, level):
        print "config.n_particles", config.n_particles
        self.n_particles = config.n_particles
        return config

    def create_pdf_lookup(self):
        pdf_lookup = {}
        for distance in arange(5.0, 0.1):
            self.normal_dist.pdf(distance)

    def update_robot_pose(self):
        """ Update the estimate of the robot's pose given the updated particles.
            There are two logical methods for this:
                (1): compute the mean pose
                (2): compute the most likely pose (i.e. the mode of the distribution)
        """
        # first make sure that the particle weights are normalized
        self.normalize_particles()

        # calculate the new robot pose from a weighted average of the particle poses
        avg_x = 0
        avg_y = 0
        avg_theta = 0

        for particle in self.particle_cloud:
            avg_x += particle.x * particle.w
            avg_y += particle.y * particle.w
            avg_theta += particle.theta * particle.w

        # convert weighted average particle pose to a quaternion
        quart_array = tf.transformations.quaternion_from_euler(0,0,avg_theta)
        pose = Pose(position = Point(x=avg_x, y=avg_y), orientation = Quaternion(x = quart_array[0], y = quart_array[1], z = quart_array[2], w = quart_array[3]) )
        self.robot_pose = pose

    def update_particles_with_odom(self, msg):
        """ Update the particles using the newly given odometry pose.
            The function computes the value delta which is a tuple (x,y,theta)
            that indicates the change in position and angle between the odometry
            when the particles were last updated and the current odometry.

            msg: this is not really needed to implement this, but is here just in case.
        """

        new_odom_xy_theta = convert_pose_to_xy_and_theta(self.odom_pose.pose)
        # compute the change in x,y,theta since our last update
        if self.current_odom_xy_theta:
            old_odom_xy_theta = self.current_odom_xy_theta
            delta = (new_odom_xy_theta[0] - self.current_odom_xy_theta[0],
                     new_odom_xy_theta[1] - self.current_odom_xy_theta[1],
                     new_odom_xy_theta[2] - self.current_odom_xy_theta[2])

            self.current_odom_xy_theta = new_odom_xy_theta
        else:
            self.current_odom_xy_theta = new_odom_xy_theta
            return

        # calculate the distance that the robot moved forward
        distance = math.sqrt(delta[0]**2 + delta[1]**2)

        for i,particle in enumerate(self.particle_cloud):
            # calculate change in particle position based on this distance
            particle_x = math.cos(particle.theta) * distance
            particle_y = math.sin(particle.theta) * distance
            particle_distance = math.sqrt(particle_x**2 + particle_y**2)

            # adding Gaussian noise to calculated particle position
            particle.x += gauss(particle_x, particle_x*0.2)
            particle.y += gauss(particle_y, particle_y*0.2)
            particle.theta += gauss(delta[2], delta[2]*0.05)


    def map_calc_range(self,x,y,theta):
        """ Difficulty Level 3: implement a ray tracing likelihood model... Let me know if you are interested """
        # TODO: nothing unless you want to try this alternate likelihood model
        pass

    def resample_particles(self):
        """ Resample the particles according to the new particle weights.
            The weights stored with each particle should define the probability that a particular
            particle is selected in the resampling step.  You may want to make use of the given helper
            function draw_random_sample.
        """
        # make sure the distribution is normalized
        self.normalize_particles()

        probabilities = []
        for particle in self.particle_cloud:
            probabilities.append(particle.w)

        # drawing a sample of particles with preference for the higher probability particles
        samples = self.draw_random_sample(self.particle_cloud,probabilities,int(self.n_particles*self.sample_factor))

        self.particle_cloud = []
        for i,particle in enumerate(samples):
            # duplicate these sampled particles to fill out the particle cloud
            self.particle_cloud.append(deepcopy(particle))
            for i in range(int(1/self.sample_factor)-1):
                noise_particle = deepcopy(particle)
                # add noise to these particle positions and orientations
                noise_particle.x = gauss(noise_particle.x, self.linear_resample_sigma)
                noise_particle.y = gauss(noise_particle.y, self.linear_resample_sigma)            
                noise_particle.theta = gauss(noise_particle.theta, self.angular_resample_sigma)            
                self.particle_cloud.append(noise_particle)

    def update_particles_with_laser(self, msg):
        """ Updates the particle weights in response to the scan contained in the msg """
        for particle in self.particle_cloud:
            weight = 0
            for i in range(0, len(msg.ranges), 5):
                distance = msg.ranges[i]
                theta = particle.theta # particle angle
                phi = i * math.pi/180 # scan angle
                
                # coordinates of the projected laser scan point
                point_x = particle.x + distance*math.cos(theta+phi)
                point_y = particle.y + distance*math.sin(theta+phi)
               
                # distance to closest obstacle from laser scan point
                distance = self.occupancy_field.get_closest_obstacle_distance(point_x, point_y)
                if math.isnan(distance):
                    distance = 5.0

                # calculate weight of particle from approximation of the normal distribution function
                point_weight = (math.sqrt(2) / math.sqrt(math.pi))  * math.exp(-1 * (distance**2 / 2*(self.model_noise_rate**2))) + self.model_noise_floor
                weight += point_weight


            particle.w = weight

    @staticmethod
    def weighted_values(values, probabilities, size):
        """ Return a random sample of size elements from the set values with the specified probabilities
            values: the values to sample from (numpy.ndarray)
            probabilities: the probability of selecting each element in values (numpy.ndarray)
            size: the number of samples
        """
        bins = np.add.accumulate(probabilities)
        return values[np.digitize(random_sample(size), bins)]

    @staticmethod
    def draw_random_sample(choices, probabilities, n):
        """ Return a random sample of n elements from the set choices with the specified probabilities
            choices: the values to sample from represented as a list
            probabilities: the probability of selecting each element in choices represented as a list
            n: the number of samples
        """

        values = np.array(range(len(choices)))
        probs = np.array(probabilities)
        bins = np.add.accumulate(probs)
        inds = values[np.digitize(random_sample(n), bins)]
        samples = []
        for i in inds:
            samples.append(deepcopy(choices[int(i)]))
        return samples

    def update_initial_pose(self, msg):
        """ Callback function to handle re-initializing the particle filter based on a pose estimate.
            These pose estimates could be generated by another ROS Node or could come from the rviz GUI """
        xy_theta = convert_pose_to_xy_and_theta(msg.pose.pose)
        self.initialize_particle_cloud(xy_theta)
        self.fix_map_to_odom_transform(msg)

    def initialize_particle_cloud(self, xy_theta=None):
        """ Initialize the particle cloud.
            Arguments
            xy_theta: a triple consisting of the mean x, y, and theta (yaw) to initialize the
                      particle cloud around.  If this input is ommitted, the odometry will be used """
        print "Initializing the particle cloud!"
        if xy_theta == None:
            xy_theta = convert_pose_to_xy_and_theta(self.odom_pose.pose)
        self.particle_cloud = []
        
        # initialized each particle with a Gaussian noise around a given pose
        # noise is dynamically configurable
        for i in range(self.n_particles):
            particle = Particle()
            particle.x = gauss(xy_theta[0],self.linear_initialization_sigma)
            particle.y = gauss(xy_theta[1],self.linear_initialization_sigma)
            particle.theta = gauss(xy_theta[2],self.angular_initialization_sigma*math.pi/180)
            self.particle_cloud.append(particle)

        self.normalize_particles()
        self.update_robot_pose()

    def normalize_particles(self):
        """ Make sure the particle weights define a valid distribution (i.e. sum to 1.0) """
        #sum all of self.particle_cloud weights

        total_weight = sum([particle.w for particle in self.particle_cloud])
        [particle.normalize_weight(total_weight) for particle in self.particle_cloud]
        
        # total_weight_after = sum([particle.w for particle in self.particle_cloud])
        # print "Total weight after: " + str(total_weight_after)
        # print (" ".join([str(point.w) for point in self.particle_cloud]))

    def publish_particles(self, msg):
        particles_conv = []
        for p in self.particle_cloud:
            particles_conv.append(p.as_pose())
        # actually send the message so that we can view it in rviz
        self.particle_pub.publish(PoseArray(header=Header(stamp=rospy.Time.now(),
                                            frame_id=self.map_frame),
                                  poses=particles_conv))

    def publish_particles_colored(self):
        """ published particles as a marker array with color mapped to particle weight
            Jonah helped with this
        """

        markers = []

        #generate color map
        weights = np.array([p.w for p in self.particle_cloud])
        cm= plt.get_cmap('jet')
        cNorm = colors.Normalize(vmin=np.min(weights), vmax=np.max(weights))
        scalarMap = cmx.ScalarMappable(norm=cNorm, cmap=cm)
        color_vals = scalarMap.to_rgba(weights, alpha=0.5)

        for i,particle in enumerate(self.particle_cloud):
            particle_color = color_vals[i, :]
            marker = self.create_arrow(i, particle.as_pose(), particle_color)
            markers.append(marker)

        self.particle_color_pub.publish(MarkerArray(markers=markers))


    def scan_received(self, msg):
        """ This is the default logic for what to do when processing scan data.
            Feel free to modify this, however, I hope it will provide a good
            guide.  The input msg is an object of type sensor_msgs/LaserScan """

        if not(self.initialized):
            # wait for initialization to complete
            return

        if not(self.tf_listener.canTransform(self.base_frame,msg.header.frame_id,msg.header.stamp)):
            # need to know how to transform the laser to the base frame
            # this will be given by either Gazebo or neato_node
            return

        if not(self.tf_listener.canTransform(self.base_frame,self.odom_frame,msg.header.stamp)):
            # need to know how to transform between base and odometric frames
            # this will eventually be published by either Gazebo or neato_node
            return

        # calculate pose of laser relative ot the robot base
        p = PoseStamped(header=Header(stamp=rospy.Time(0),
                                      frame_id=msg.header.frame_id))
        self.laser_pose = self.tf_listener.transformPose(self.base_frame,p)

        # find out where the robot thinks it is based on its odometry
        p = PoseStamped(header=Header(stamp=msg.header.stamp,
                                      frame_id=self.base_frame),
                        pose=Pose())
        self.odom_pose = self.tf_listener.transformPose(self.odom_frame, p)
        # store the the odometry pose in a more convenient format (x,y,theta)
        new_odom_xy_theta = convert_pose_to_xy_and_theta(self.odom_pose.pose)

        if not(self.particle_cloud):
            # now that we have all of the necessary transforms we can update the particle cloud
            self.initialize_particle_cloud()
            # cache the last odometric pose so we can only update our particle filter if we move more than self.d_thresh or self.a_thresh
            self.current_odom_xy_theta = new_odom_xy_theta
            # update our map to odom transform now that the particles are initialized
            self.fix_map_to_odom_transform(msg)
        
        # elif not self.current_odom_xy_theta:
        #     self.current_odom_xy_theta = new_odom_xy_theta
        

        elif self.current_odom_xy_theta:
            if (math.fabs(new_odom_xy_theta[0] - self.current_odom_xy_theta[0]) > self.d_thresh or
              math.fabs(new_odom_xy_theta[1] - self.current_odom_xy_theta[1]) > self.d_thresh or
              math.fabs(new_odom_xy_theta[2] - self.current_odom_xy_theta[2]) > self.a_thresh):
                # we have moved far enough to do an update!
                self.update_particles_with_odom(msg)    # update based on odometry
                self.update_particles_with_laser(msg)   # update based on laser scan
                self.update_robot_pose()                # update robot's pose
                self.resample_particles()               # resample particles to focus on areas of high density
                self.fix_map_to_odom_transform(msg)     # update map to odom transform now that we have new particles
        # publish particles (so things like rviz can see them)
        #self.publish_particles(msg)
        self.publish_particles_colored()


    def fix_map_to_odom_transform(self, msg):
        """ This method constantly updates the offset of the map and 
            odometry coordinate systems based on the latest results from
            the localizer
            TODO: if you want to learn a lot about tf, reimplement this... I can provide
                  you with some hints as to what is going on here. """
        (translation, rotation) = convert_pose_inverse_transform(self.robot_pose)
        p = PoseStamped(pose=convert_translation_rotation_to_pose(translation,rotation),
                        header=Header(stamp=msg.header.stamp,frame_id=self.base_frame))
        self.tf_listener.waitForTransform(self.base_frame, self.odom_frame, msg.header.stamp, rospy.Duration(1.0))
        self.odom_to_map = self.tf_listener.transformPose(self.odom_frame, p)
        (self.translation, self.rotation) = convert_pose_inverse_transform(self.odom_to_map.pose)

    def broadcast_last_transform(self):
        """ Make sure that we are always broadcasting the last map
            to odom transformation.  This is necessary so things like
            move_base can work properly. """
        if not(hasattr(self,'translation') and hasattr(self,'rotation')):
            return
        self.tf_broadcaster.sendTransform(self.translation,
                                          self.rotation,
                                          rospy.get_rostime(),
                                          self.odom_frame,
                                          self.map_frame)

    def visualize_particles(self):
        """ Not very helpful attemp to visualize particles with a heat map """
        x = np.array([])
        y = np.array([])
        for particle in self.particle_cloud:
            x = np.append(x, particle.x)
            y = np.append(y, particle.y)

        heatmap, xedges, yedges = np.histogram2d(x, y, bins=20)
        extent = [xedges[0], xedges[-1], yedges[0], yedges[-1]]

        self.fig.clf()

        subplot = self.fig.add_subplot(1,1,1)
        subplot.imshow(heatmap.T, extent=extent, origin='lower')
        plt.draw()
        plt.pause(.01)

    def create_arrow(self, index, pose, color):
        marker = Marker(
            type = Marker.ARROW,
            id = index,
            header = Header( 
                stamp = rospy.Time.now(),
                frame_id = self.map_frame
                ),
            pose = pose,
            scale = Vector3(0.4, 0.05, 0.05),
            color = ColorRGBA(color[0], color[1], color[2], color[3])
            )
        return marker


if __name__ == '__main__':
    n = ParticleFilter()
    r = rospy.Rate(5)

    while not(rospy.is_shutdown()):
        # in the main loop all we do is continuously broadcast the latest map to odom transform
        n.broadcast_last_transform()
        #n.visualize_particles()
        try:
            r.sleep()
        except rospy.exceptions.ROSTimeMovedBackwardsException:
            print "time went backwards"
