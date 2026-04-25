# MicroNinjas - Organoid Sorting

<img src="https://github.com/alandolt/organoid-shuffler/blob/master/images/microninjas.png" width="300"/>

*EMBO Hackathon Hack your Microscope 2026 in Oeiras, Portugal*

This project was developed during a hands-on hackathon focused on building open, functional microscopy and microfluidic systems from the ground up. The goal was not just to prototype ideas, but to design, build, and test complete working pipelines under real constraints.

Participants: Alex Landolt, Mai Rahmoon, Olyssa Sperling, Kartik Totlani, Enrico Piperno

Tutors: Sunil Kumar, Dumisile Lumkwana 

## The challenge
We tackled size-based sorting of organoids in microfluidic flow.

The task required:

* Detecting organoids in real time inside a microfluidic chip
* Classifying them based on size
* Actively sorting them into multiple outlets
* Closing the loop with automated control and verification

We built a closed-loop system that integrates: flow → image → classify → actuate → verify

## Brainstorming
<img src="https://github.com/alandolt/organoid-shuffler/blob/master/images/chip_design.png" width="600"/>

## Fabrication of microfluidic chip

<img src="https://github.com/alandolt/organoid-shuffler/blob/master/images/lasercutting1.png" width="600"/>

<img src="https://github.com/alandolt/organoid-shuffler/blob/master/images/soft_lithography.png" width="600"/>

## Experimental setup
The final experimental setup consists of:
* automatable syringe pumps by the [Poseidon design](https://github.com/pachterlab/poseidon)
* a [microfluidic chip by IBIDI](https://ibidi.com/channel-slides/50--slide-i-luer.html) mounted on an [OpenFrame fluorescence microscope](https://cairn-research.co.uk/product/openframe-microscope/) by cairn research
* the sorting mechanism crafted by a one-axis translation stage, and a 3D printed 0.5 ml Eppendorf tube adapter.
The parts are connected using silicon and teflon tubing.

## Microfluidic actuation
<img src="https://github.com/alandolt/organoid-shuffler/blob/master/images/actuation.png" width="600"/>
We actuated our microfluidic chips with the [open-source Poseidon design](https://github.com/pachterlab/poseidon). 
We wrote custom scripts to actuate the stepper motors and then calibrated the pumps to convert from steps to mm (travel stage dependent) and then mm to ml (syringe dependent). 
<img src="https://github.com/alandolt/organoid-shuffler/blob/master/images/sorting.png" width="600"/>
This way we were able control flow precisely up to 1 ul/s. The pump control was done manually first with cnc_stepper_motor.py and cnc_stepper_motor.ino flashed on the Arduino UNO. 

## Image analysis
<img src="https://github.com/alandolt/organoid-shuffler/blob/master/images/image_analysis.png" width="600"/>
Using the syringe pumps we achieved a controlled flow of organoids through the chip that was imaged in real-time at 1 f/s. 
In order to use the pipeline, please follow the instructions in the notebook Pipeline.ipynb, which will guide you through the different steps of the pipeline.
The pipeline does the following steps:
* Snaps an image from the microscope
* Segments the image using the trained Convpaint model
* Cleans up the segmentation mask using some morphological operations
* Measures the size of the detected organoids (the segmented objects in class 2)
* Tracks the detected organoids across frames using trackpy
* If an organoid is detected that is bigger than 100 pixels in the field of view, it moves it to eppendorf slot 1 for 30s, otherwise it moves it to slot 0.
* All detections and sorting decisions are logged in a parquet file for later analysis.
* The flow in the chip is controlled by setting a flowrate of 0.001 ml/min in the pump that controls the flow rate in the chip (the one connected to the inlet). You can directly set the flowrate through the jupyter notebook.

## Results
[![](https://img.youtube.com/vi/6NS1tuH9CTA/0.jpg)](https://youtu.be/6NS1tuH9CTA "Click to play on Youtube.com")

## Discussion
The organoids were smaller and rarer than expected since the sample was very dilute. 

In the end we managed to find some, train the classification model on them and start the sorting procedure.

In the future we can use the speed calculation in order to calculate the time of the organoid reaching the end of the tubing, matching the exact time for the organoid to drop into the sorting Eppendorf tubings.

<img src="https://github.com/alandolt/organoid-shuffler/blob/master/images/sketch.png" width="600"/>
